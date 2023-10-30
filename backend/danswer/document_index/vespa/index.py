import concurrent.futures
import json
import string
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import cast

import requests
from requests import HTTPError
from requests import Response
from retry import retry

from danswer.configs.app_configs import DOC_TIME_DECAY
from danswer.configs.app_configs import DOCUMENT_INDEX_NAME
from danswer.configs.app_configs import EDIT_KEYWORD_QUERY
from danswer.configs.app_configs import FAVOR_RECENT_DECAY_MULTIPLIER
from danswer.configs.app_configs import NUM_RETURNED_HITS
from danswer.configs.app_configs import VESPA_DEPLOYMENT_ZIP
from danswer.configs.app_configs import VESPA_HOST
from danswer.configs.app_configs import VESPA_PORT
from danswer.configs.app_configs import VESPA_TENANT_PORT
from danswer.configs.constants import ACCESS_CONTROL_LIST
from danswer.configs.constants import BLURB
from danswer.configs.constants import BOOST
from danswer.configs.constants import CHUNK_ID
from danswer.configs.constants import CONTENT
from danswer.configs.constants import DEFAULT_BOOST
from danswer.configs.constants import DOC_UPDATED_AT
from danswer.configs.constants import DOCUMENT_ID
from danswer.configs.constants import DOCUMENT_SETS
from danswer.configs.constants import EMBEDDINGS
from danswer.configs.constants import HIDDEN
from danswer.configs.constants import METADATA
from danswer.configs.constants import PRIMARY_OWNERS
from danswer.configs.constants import RECENCY_BIAS
from danswer.configs.constants import SECONDARY_OWNERS
from danswer.configs.constants import SECTION_CONTINUATION
from danswer.configs.constants import SEMANTIC_IDENTIFIER
from danswer.configs.constants import SOURCE_LINKS
from danswer.configs.constants import SOURCE_TYPE
from danswer.configs.constants import TITLE
from danswer.configs.model_configs import SEARCH_DISTANCE_CUTOFF
from danswer.document_index.document_index_utils import get_uuid_from_chunk
from danswer.document_index.interfaces import DocumentIndex
from danswer.document_index.interfaces import DocumentInsertionRecord
from danswer.document_index.interfaces import UpdateRequest
from danswer.document_index.vespa.utils import remove_invalid_unicode_chars
from danswer.indexing.models import DocMetadataAwareIndexChunk
from danswer.indexing.models import InferenceChunk
from danswer.search.models import IndexFilters
from danswer.search.search_runner import embed_query
from danswer.search.search_runner import query_processing
from danswer.search.search_runner import remove_stop_words
from danswer.utils.batching import batch_generator
from danswer.utils.logger import setup_logger

logger = setup_logger()


VESPA_CONFIG_SERVER_URL = f"http://{VESPA_HOST}:{VESPA_TENANT_PORT}"
VESPA_APP_CONTAINER_URL = f"http://{VESPA_HOST}:{VESPA_PORT}"
VESPA_APPLICATION_ENDPOINT = f"{VESPA_CONFIG_SERVER_URL}/application/v2"
# danswer_chunk below is defined in vespa/app_configs/schemas/danswer_chunk.sd
DOCUMENT_ID_ENDPOINT = (
    f"{VESPA_APP_CONTAINER_URL}/document/v1/default/danswer_chunk/docid"
)
SEARCH_ENDPOINT = f"{VESPA_APP_CONTAINER_URL}/search/"
_BATCH_SIZE = 128  # Specific to Vespa
_NUM_THREADS = (
    32  # since Vespa doesn't allow batching of inserts / updates, we use threads
)
# Specific to Vespa, needed for highlighting matching keywords / section
CONTENT_SUMMARY = "content_summary"


@dataclass
class _VespaUpdateRequest:
    document_id: str
    url: str
    update_request: dict[str, dict]


def _does_document_exist(
    doc_chunk_id: str,
) -> bool:
    """Returns whether the document already exists and the users/group whitelists"""
    doc_fetch_response = requests.get(f"{DOCUMENT_ID_ENDPOINT}/{doc_chunk_id}")
    if doc_fetch_response.status_code == 404:
        return False

    if doc_fetch_response.status_code != 200:
        raise RuntimeError(
            f"Unexpected fetch document by ID value from Vespa "
            f"with error {doc_fetch_response.status_code}"
        )
    return True


def _vespa_get_updated_at_attribute(t: datetime | None) -> int | None:
    if not t:
        return None

    if t.tzinfo != timezone.utc:
        raise ValueError("Connectors must provide document update time in UTC")

    return int(t.timestamp())


def _get_vespa_chunk_ids_by_document_id(
    document_id: str, hits_per_page: int = _BATCH_SIZE
) -> list[str]:
    offset = 0
    doc_chunk_ids = []
    params: dict[str, int | str] = {
        "yql": f"select documentid from {DOCUMENT_INDEX_NAME} where document_id contains '{document_id}'",
        "timeout": "10s",
        "offset": offset,
        "hits": hits_per_page,
    }
    while True:
        results = requests.get(SEARCH_ENDPOINT, params=params).json()
        hits = results["root"].get("children", [])

        doc_chunk_ids.extend(
            [hit["fields"]["documentid"].split("::", 1)[-1] for hit in hits]
        )
        params["offset"] += hits_per_page  # type: ignore

        if len(hits) < hits_per_page:
            break
    return doc_chunk_ids


def _delete_vespa_doc_chunks(document_id: str) -> bool:
    doc_chunk_ids = _get_vespa_chunk_ids_by_document_id(document_id)

    failed = False
    for chunk_id in doc_chunk_ids:
        success = (
            requests.delete(f"{DOCUMENT_ID_ENDPOINT}/{chunk_id}").status_code == 200
        )
        if not success:
            failed = True
            logger.error(f"Failed to delete chunk: {chunk_id}")

    return not failed


@retry(tries=3, delay=1, backoff=2)
def _index_vespa_chunk(
    chunk: DocMetadataAwareIndexChunk, already_existing_documents: set[str]
) -> bool:
    json_header = {
        "Content-Type": "application/json",
    }
    document = chunk.source_document
    # No minichunk documents in vespa, minichunk vectors are stored in the chunk itself
    vespa_chunk_id = str(get_uuid_from_chunk(chunk))

    # Delete all chunks related to the document if (1) it already exists and
    # (2) this is our first time running into it during this indexing attempt
    chunk_exists = _does_document_exist(vespa_chunk_id)
    if chunk_exists and document.id not in already_existing_documents:
        deletion_success = _delete_vespa_doc_chunks(document.id)
        if not deletion_success:
            raise RuntimeError(
                f"Failed to delete pre-existing chunks for with document with id: {document.id}"
            )

    embeddings = chunk.embeddings
    embeddings_name_vector_map = {"full_chunk": embeddings.full_embedding}
    if embeddings.mini_chunk_embeddings:
        for ind, m_c_embed in enumerate(embeddings.mini_chunk_embeddings):
            embeddings_name_vector_map[f"mini_chunk_{ind}"] = m_c_embed

    vespa_document_fields = {
        DOCUMENT_ID: document.id,
        CHUNK_ID: chunk.chunk_id,
        BLURB: chunk.blurb,
        # this duplication of `content` is needed for keyword highlighting :(
        CONTENT: chunk.content,
        CONTENT_SUMMARY: chunk.content,
        SOURCE_TYPE: str(document.source.value),
        SOURCE_LINKS: json.dumps(chunk.source_links),
        SEMANTIC_IDENTIFIER: document.semantic_identifier,
        TITLE: document.get_title_for_document_index(),
        SECTION_CONTINUATION: chunk.section_continuation,
        METADATA: json.dumps(document.metadata),
        EMBEDDINGS: embeddings_name_vector_map,
        BOOST: DEFAULT_BOOST,
        DOC_UPDATED_AT: _vespa_get_updated_at_attribute(document.doc_updated_at),
        PRIMARY_OWNERS: document.primary_owners,
        SECONDARY_OWNERS: document.secondary_owners,
        # the only `set` vespa has is `weightedset`, so we have to give each
        # element an arbitrary weight
        ACCESS_CONTROL_LIST: {acl_entry: 1 for acl_entry in chunk.access.to_acl()},
        DOCUMENT_SETS: {document_set: 1 for document_set in chunk.document_sets},
    }

    def _index_chunk(
        url: str,
        headers: dict[str, str],
        fields: dict[str, Any],
        log_error: bool = True,
    ) -> Response:
        logger.debug(f'Indexing to URL "{url}"')
        res = requests.post(url, headers=headers, json={"fields": fields})
        try:
            res.raise_for_status()
            return res
        except Exception as e:
            if log_error:
                logger.error(
                    f"Failed to index document: '{document.id}'. Got response: '{res.text}'"
                )
            raise e

    vespa_url = f"{DOCUMENT_ID_ENDPOINT}/{vespa_chunk_id}"
    try:
        _index_chunk(
            url=vespa_url,
            headers=json_header,
            fields=vespa_document_fields,
            log_error=False,
        )
    except HTTPError as e:
        if cast(Response, e.response).status_code != 400:
            raise e

        # if it's a 400 response, try again with invalid unicode chars removed
        # only doing this on error to avoid having to go through the content
        # char by char every time
        vespa_document_fields[BLURB] = remove_invalid_unicode_chars(
            cast(str, vespa_document_fields[BLURB])
        )
        vespa_document_fields[SEMANTIC_IDENTIFIER] = remove_invalid_unicode_chars(
            cast(str, vespa_document_fields[SEMANTIC_IDENTIFIER])
        )
        vespa_document_fields[CONTENT] = remove_invalid_unicode_chars(
            cast(str, vespa_document_fields[CONTENT])
        )
        vespa_document_fields[CONTENT_SUMMARY] = remove_invalid_unicode_chars(
            cast(str, vespa_document_fields[CONTENT_SUMMARY])
        )
        _index_chunk(
            url=vespa_url,
            headers=json_header,
            fields=vespa_document_fields,
            log_error=True,
        )

    return chunk_exists


def _index_vespa_chunks(
    chunks: list[DocMetadataAwareIndexChunk],
) -> set[DocumentInsertionRecord]:
    insertion_records: set[DocumentInsertionRecord] = set()
    # document ids of documents that existed BEFORE this indexing
    already_existing_documents: set[str] = set()

    # use threads to parallelize since Vespa doesn't allow batching of updates
    with concurrent.futures.ThreadPoolExecutor(max_workers=_NUM_THREADS) as executor:
        for chunk_batch in batch_generator(chunks, _BATCH_SIZE):
            future_to_chunk = {
                executor.submit(
                    _index_vespa_chunk, chunk, already_existing_documents
                ): chunk
                for chunk in chunk_batch
            }
            for future in concurrent.futures.as_completed(future_to_chunk):
                chunk = future_to_chunk[future]
                chunk_already_existed = future.result()
                if chunk_already_existed:
                    already_existing_documents.add(chunk.source_document.id)

                # In the logic below, we check if the chunk comes from a doc that has already been
                # added to already_existing_document. This works because the chunks are ordered
                # and because the Document chunks are not separated into different batches.
                # The first chunk is processed first and if it exists, then its entire document
                # is marked as already existing, so if the document length increases and new chunks
                # are added, they must come last in processing and the doc would already be in
                # already existing documents.
                insertion_records.add(
                    DocumentInsertionRecord(
                        document_id=chunk.source_document.id,
                        already_existed=chunk.source_document.id
                        in already_existing_documents,
                    )
                )

    return insertion_records


def _build_vespa_filters(filters: IndexFilters, include_hidden: bool = False) -> str:
    def _build_or_filters(key: str, vals: list[str] | None) -> str:
        if vals is None:
            return ""

        valid_vals = [val for val in vals if val]
        if not key or not valid_vals:
            return ""

        eq_elems = [f'{key} contains "{elem}"' for elem in valid_vals]
        or_clause = " or ".join(eq_elems)
        return f"({or_clause}) and "

    def _build_time_filter(
        cutoff: datetime | None,
        # Slightly over 3 Months, approximately 1 fiscal quarter
        untimed_doc_cutoff: timedelta = timedelta(days=92),
    ) -> str:
        if not cutoff:
            return ""

        # For Documents that don't have an updated at, filter them out for queries asking for
        # very recent documents (3 months) default. Documents that don't have an updated at
        # time are assigned 3 months for time decay value
        include_untimed = datetime.now(timezone.utc) - untimed_doc_cutoff > cutoff
        cutoff_secs = int(cutoff.timestamp())

        if include_untimed:
            # Documents without updated_at are assigned -1 as their date
            return f"!({DOC_UPDATED_AT} < {cutoff_secs}) and "

        return f"({DOC_UPDATED_AT} >= {cutoff_secs}) and "

    filter_str = f"!({HIDDEN}=true) and " if not include_hidden else ""

    # CAREFUL touching this one, currently there is no second ACL double-check post retrieval
    filter_str += _build_or_filters(ACCESS_CONTROL_LIST, filters.access_control_list)

    filter_str += _build_or_filters(SOURCE_TYPE, filters.source_type)

    filter_str += _build_or_filters(DOCUMENT_SETS, filters.document_set)

    filter_str += _build_time_filter(filters.time_cutoff)

    return filter_str


def _process_dynamic_summary(
    dynamic_summary: str, max_summary_length: int = 400
) -> list[str]:
    if not dynamic_summary:
        return []

    current_length = 0
    processed_summary: list[str] = []
    for summary_section in dynamic_summary.split("<sep />"):
        # if we're past the desired max length, break at the last word
        if current_length + len(summary_section) >= max_summary_length:
            summary_section = summary_section[: max_summary_length - current_length]
            summary_section = summary_section.lstrip()  # remove any leading whitespace

            # handle the case where the truncated section is either just a
            # single (partial) word or if it's empty
            first_space = summary_section.find(" ")
            if first_space == -1:
                # add ``...`` to previous section
                if processed_summary:
                    processed_summary[-1] += "..."
                break

            # handle the valid truncated section case
            summary_section = summary_section.rsplit(" ", 1)[0]
            if summary_section[-1] in string.punctuation:
                summary_section = summary_section[:-1]
            summary_section += "..."
            processed_summary.append(summary_section)
            break

        processed_summary.append(summary_section)
        current_length += len(summary_section)

    return processed_summary


def _vespa_hit_to_inference_chunk(hit: dict[str, Any]) -> InferenceChunk:
    fields = cast(dict[str, Any], hit["fields"])

    # parse fields that are stored as strings, but are really json / datetime
    metadata = json.loads(fields[METADATA]) if METADATA in fields else {}
    updated_at = (
        datetime.fromtimestamp(fields[DOC_UPDATED_AT], tz=timezone.utc)
        if DOC_UPDATED_AT in fields
        else None
    )
    match_highlights = _process_dynamic_summary(
        # fallback to regular `content` if the `content_summary` field
        # isn't present
        dynamic_summary=hit["fields"].get(CONTENT_SUMMARY, hit["fields"][CONTENT]),
    )
    semantic_identifier = fields.get(SEMANTIC_IDENTIFIER, "")
    if not semantic_identifier:
        logger.error(
            f"Chunk with blurb: {fields.get(BLURB, 'Unknown')[:50]}... has no Semantic Identifier"
        )
    source_links = fields.get(SOURCE_LINKS, {})
    source_links_dict_unprocessed = (
        json.loads(source_links) if isinstance(source_links, str) else source_links
    )
    source_links_dict = {
        int(k): v
        for k, v in cast(dict[str, str], source_links_dict_unprocessed).items()
    }

    return InferenceChunk(
        chunk_id=fields[CHUNK_ID],
        blurb=fields[BLURB],
        content=fields[CONTENT],
        source_links=source_links_dict,
        section_continuation=fields[SECTION_CONTINUATION],
        document_id=fields[DOCUMENT_ID],
        source_type=fields[SOURCE_TYPE],
        semantic_identifier=fields[SEMANTIC_IDENTIFIER],
        boost=fields.get(BOOST, 1),
        recency_bias=fields["matchfeatures"][RECENCY_BIAS],
        score=hit["relevance"],
        hidden=fields.get(HIDDEN, False),
        metadata=metadata,
        match_highlights=match_highlights,
        updated_at=updated_at,
    )


def _query_vespa(query_params: Mapping[str, str | int]) -> list[InferenceChunk]:
    if "query" in query_params and not cast(str, query_params["query"]).strip():
        raise ValueError("No/empty query received")
    response = requests.get(SEARCH_ENDPOINT, params=query_params)
    response.raise_for_status()

    hits = response.json()["root"].get("children", [])

    for hit in hits:
        if hit["fields"].get(CONTENT) is None:
            identifier = hit["fields"].get("documentid") or hit["id"]
            logger.error(
                f"Vespa Index with Vespa ID {identifier} has no contents. "
                f"This is invalid because the vector is not meaningful and keywordsearch cannot "
                f"fetch this document"
            )

    filtered_hits = [hit for hit in hits if hit["fields"].get(CONTENT) is not None]

    inference_chunks = [_vespa_hit_to_inference_chunk(hit) for hit in filtered_hits]
    return inference_chunks


class VespaIndex(DocumentIndex):
    yql_base = (
        f"select "
        f"documentid, "
        f"{DOCUMENT_ID}, "
        f"{CHUNK_ID}, "
        f"{BLURB}, "
        f"{CONTENT}, "
        f"{SOURCE_TYPE}, "
        f"{SOURCE_LINKS}, "
        f"{SEMANTIC_IDENTIFIER}, "
        f"{SECTION_CONTINUATION}, "
        f"{BOOST}, "
        f"{HIDDEN}, "
        f"{DOC_UPDATED_AT}, "
        f"{METADATA}, "
        f"{CONTENT_SUMMARY} "
        f"from {DOCUMENT_INDEX_NAME} where "
    )

    def __init__(self, deployment_zip: str = VESPA_DEPLOYMENT_ZIP) -> None:
        # Vespa index name isn't configurable via code alone because of the config .sd file that needs
        # to be updated + zipped + deployed, not supporting the option for simplicity
        self.deployment_zip = deployment_zip

    def ensure_indices_exist(self) -> None:
        """Verifying indices is more involved as there is no good way to
        verify the deployed app against the zip locally. But deploying the latest app.zip will ensure that
        the index is up-to-date with the expected schema and this does not erase the existing index.
        If the changes cannot be applied without conflict with existing data, it will fail with a non 200
        """
        deploy_url = f"{VESPA_APPLICATION_ENDPOINT}/tenant/default/prepareandactivate"
        logger.debug(f"Sending Vespa zip to {deploy_url}")
        headers = {"Content-Type": "application/zip"}
        with open(self.deployment_zip, "rb") as f:
            response = requests.post(deploy_url, headers=headers, data=f)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Failed to prepare Vespa Danswer Index. Response: {response.text}"
                )

    def index(
        self,
        chunks: list[DocMetadataAwareIndexChunk],
    ) -> set[DocumentInsertionRecord]:
        return _index_vespa_chunks(chunks=chunks)

    @staticmethod
    def _apply_updates_batched(
        updates: list[_VespaUpdateRequest],
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        """Runs a batch of updates in parallel via the ThreadPoolExecutor."""

        def _update_chunk(update: _VespaUpdateRequest) -> Response:
            update_body = json.dumps(update.update_request)
            logger.debug(
                f"Updating with request to {update.url} with body {update_body}"
            )
            return requests.put(
                update.url,
                headers={"Content-Type": "application/json"},
                data=update_body,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_NUM_THREADS
        ) as executor:
            for update_batch in batch_generator(updates, batch_size):
                future_to_document_id = {
                    executor.submit(
                        _update_chunk,
                        update,
                    ): update.document_id
                    for update in update_batch
                }
                for future in concurrent.futures.as_completed(future_to_document_id):
                    res = future.result()
                    try:
                        res.raise_for_status()
                    except requests.HTTPError as e:
                        failure_msg = f"Failed to update document: {future_to_document_id[future]}"
                        raise requests.HTTPError(failure_msg) from e

    def update(self, update_requests: list[UpdateRequest]) -> None:
        logger.info(f"Updating {len(update_requests)} documents in Vespa")
        start = time.time()

        processed_updates_requests: list[_VespaUpdateRequest] = []
        for update_request in update_requests:
            update_dict: dict[str, dict] = {"fields": {}}
            if update_request.boost is not None:
                update_dict["fields"][BOOST] = {"assign": update_request.boost}
            if update_request.document_sets is not None:
                update_dict["fields"][DOCUMENT_SETS] = {
                    "assign": {
                        document_set: 1 for document_set in update_request.document_sets
                    }
                }
            if update_request.access is not None:
                update_dict["fields"][ACCESS_CONTROL_LIST] = {
                    "assign": {
                        acl_entry: 1 for acl_entry in update_request.access.to_acl()
                    }
                }
            if update_request.hidden is not None:
                update_dict["fields"][HIDDEN] = {"assign": update_request.hidden}

            if not update_dict["fields"]:
                logger.error("Update request received but nothing to update")
                continue

            for document_id in update_request.document_ids:
                for doc_chunk_id in _get_vespa_chunk_ids_by_document_id(document_id):
                    processed_updates_requests.append(
                        _VespaUpdateRequest(
                            document_id=document_id,
                            url=f"{DOCUMENT_ID_ENDPOINT}/{doc_chunk_id}",
                            update_request=update_dict,
                        )
                    )

        self._apply_updates_batched(processed_updates_requests)
        logger.info(
            "Finished updating Vespa documents in %s seconds", time.time() - start
        )

    def delete(self, doc_ids: list[str]) -> None:
        logger.info(f"Deleting {len(doc_ids)} documents from Vespa")
        for doc_id in doc_ids:
            success = _delete_vespa_doc_chunks(doc_id)
            if not success:
                raise RuntimeError(
                    f"Unable to delete document with document id: {doc_id}"
                )

    def keyword_retrieval(
        self,
        query: str,
        filters: IndexFilters,
        favor_recent: bool,
        num_to_retrieve: int = NUM_RETURNED_HITS,
        edit_keyword_query: bool = EDIT_KEYWORD_QUERY,
    ) -> list[InferenceChunk]:
        decay_multiplier = FAVOR_RECENT_DECAY_MULTIPLIER if favor_recent else 1
        vespa_where_clauses = _build_vespa_filters(filters)
        yql = (
            VespaIndex.yql_base
            + vespa_where_clauses
            # `({defaultIndex: "content_summary"}userInput(@query))` section is
            # needed for highlighting while the N-gram highlighting is broken /
            # not working as desired
            + '({grammar: "weakAnd"}userInput(@query) '
            + f'or ({{defaultIndex: "{CONTENT_SUMMARY}"}}userInput(@query)))'
        )

        final_query = query_processing(query) if edit_keyword_query else query

        params: dict[str, str | int] = {
            "yql": yql,
            "query": final_query,
            "input.query(decay_factor)": str(DOC_TIME_DECAY * decay_multiplier),
            "hits": num_to_retrieve,
            "offset": 0,
            "ranking.profile": "keyword_search",
        }

        return _query_vespa(params)

    def semantic_retrieval(
        self,
        query: str,
        filters: IndexFilters,
        favor_recent: bool,
        num_to_retrieve: int = NUM_RETURNED_HITS,
        distance_cutoff: float | None = SEARCH_DISTANCE_CUTOFF,
        edit_keyword_query: bool = EDIT_KEYWORD_QUERY,
    ) -> list[InferenceChunk]:
        decay_multiplier = FAVOR_RECENT_DECAY_MULTIPLIER if favor_recent else 1
        vespa_where_clauses = _build_vespa_filters(filters)
        yql = (
            VespaIndex.yql_base
            + vespa_where_clauses
            + f"(({{targetHits: {10 * num_to_retrieve}}}nearestNeighbor(embeddings, query_embedding)) "
            # `({defaultIndex: "content_summary"}userInput(@query))` section is
            # needed for highlighting while the N-gram highlighting is broken /
            # not working as desired
            + f'or ({{defaultIndex: "{CONTENT_SUMMARY}"}}userInput(@query)))'
        )

        query_embedding = embed_query(query)

        query_keywords = (
            " ".join(remove_stop_words(query)) if edit_keyword_query else query
        )

        params: dict[str, str | int] = {
            "yql": yql,
            "query": query_keywords,  # Needed for highlighting
            "input.query(query_embedding)": str(query_embedding),
            "input.query(decay_factor)": str(DOC_TIME_DECAY * decay_multiplier),
            "hits": num_to_retrieve,
            "offset": 0,
            "ranking.profile": "semantic_search",
        }

        return _query_vespa(params)

    def hybrid_retrieval(
        self,
        query: str,
        filters: IndexFilters,
        favor_recent: bool,
        num_to_retrieve: int,
        distance_cutoff: float | None = SEARCH_DISTANCE_CUTOFF,
        edit_keyword_query: bool = EDIT_KEYWORD_QUERY,
    ) -> list[InferenceChunk]:
        decay_multiplier = FAVOR_RECENT_DECAY_MULTIPLIER if favor_recent else 1
        vespa_where_clauses = _build_vespa_filters(filters)
        # Needs to be at least as much as the value set in Vespa schema config
        target_hits = max(10 * num_to_retrieve, 1000)
        yql = (
            VespaIndex.yql_base
            + vespa_where_clauses
            + f"(({{targetHits: {target_hits}}}nearestNeighbor(embeddings, query_embedding)) "
            + 'or ({grammar: "weakAnd"}userInput(@query)) '
            + f'or ({{defaultIndex: "{CONTENT_SUMMARY}"}}userInput(@query)))'
        )

        query_embedding = embed_query(query)

        query_keywords = (
            " ".join(remove_stop_words(query)) if edit_keyword_query else query
        )

        params: dict[str, str | int] = {
            "yql": yql,
            "query": query_keywords,
            "input.query(query_embedding)": str(query_embedding),
            "input.query(decay_factor)": str(DOC_TIME_DECAY * decay_multiplier),
            "hits": num_to_retrieve,
            "offset": 0,
            "ranking.profile": "hybrid_search",
        }

        return _query_vespa(params)

    def admin_retrieval(
        self,
        query: str,
        filters: IndexFilters,
        num_to_retrieve: int = NUM_RETURNED_HITS,
    ) -> list[InferenceChunk]:
        vespa_where_clauses = _build_vespa_filters(filters, include_hidden=True)
        yql = (
            VespaIndex.yql_base
            + vespa_where_clauses
            + '({grammar: "weakAnd"}userInput(@query) '
            # `({defaultIndex: "content_summary"}userInput(@query))` section is
            # needed for highlighting while the N-gram highlighting is broken /
            # not working as desired
            + f'or ({{defaultIndex: "{CONTENT_SUMMARY}"}}userInput(@query)))'
        )

        params: dict[str, str | int] = {
            "yql": yql,
            "query": query,
            "hits": num_to_retrieve,
            "offset": 0,
            "ranking.profile": "admin_search",
        }

        return _query_vespa(params)
