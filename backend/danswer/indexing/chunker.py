import abc
from collections.abc import Callable

from llama_index.text_splitter import SentenceSplitter
from transformers import AutoTokenizer  # type:ignore

from danswer.configs.app_configs import BLURB_SIZE
from danswer.configs.app_configs import CHUNK_OVERLAP
from danswer.configs.app_configs import CHUNK_SIZE
from danswer.configs.app_configs import MINI_CHUNK_SIZE
from danswer.connectors.models import Document
from danswer.connectors.models import Section
from danswer.indexing.models import DocAwareChunk
from danswer.search.search_nlp_models import get_default_tokenizer
from danswer.utils.text_processing import shared_precompare_cleanup


SECTION_SEPARATOR = "\n\n"
ChunkFunc = Callable[[Document], list[DocAwareChunk]]


def extract_blurb(text: str, blurb_size: int) -> str:
    token_count_func = get_default_tokenizer().tokenize
    blurb_splitter = SentenceSplitter(
        tokenizer=token_count_func, chunk_size=blurb_size, chunk_overlap=0
    )

    return blurb_splitter.split_text(text)[0]


def chunk_large_section(
    section: Section,
    document: Document,
    start_chunk_id: int,
    tokenizer: AutoTokenizer,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    blurb_size: int = BLURB_SIZE,
) -> list[DocAwareChunk]:
    section_text = section.text
    blurb = extract_blurb(section_text, blurb_size)

    sentence_aware_splitter = SentenceSplitter(
        tokenizer=tokenizer.tokenize, chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )

    split_texts = sentence_aware_splitter.split_text(section_text)

    chunks = [
        DocAwareChunk(
            source_document=document,
            chunk_id=start_chunk_id + chunk_ind,
            blurb=blurb,
            content=chunk_str,
            source_links={0: section.link},
            section_continuation=(chunk_ind != 0),
        )
        for chunk_ind, chunk_str in enumerate(split_texts)
    ]
    return chunks


def chunk_document(
    document: Document,
    chunk_tok_size: int = CHUNK_SIZE,
    subsection_overlap: int = CHUNK_OVERLAP,
    blurb_size: int = BLURB_SIZE,
) -> list[DocAwareChunk]:
    tokenizer = get_default_tokenizer()

    chunks: list[DocAwareChunk] = []
    link_offsets: dict[int, str] = {}
    chunk_text = ""
    for section in document.sections:
        section_tok_length = len(tokenizer.tokenize(section.text))
        current_tok_length = len(tokenizer.tokenize(chunk_text))
        curr_offset_len = len(shared_precompare_cleanup(chunk_text))

        # Large sections are considered self-contained/unique therefore they start a new chunk and are not concatenated
        # at the end by other sections
        if section_tok_length > chunk_tok_size:
            if chunk_text:
                chunks.append(
                    DocAwareChunk(
                        source_document=document,
                        chunk_id=len(chunks),
                        blurb=extract_blurb(chunk_text, blurb_size),
                        content=chunk_text,
                        source_links=link_offsets,
                        section_continuation=False,
                    )
                )
                link_offsets = {}
                chunk_text = ""

            large_section_chunks = chunk_large_section(
                section=section,
                document=document,
                start_chunk_id=len(chunks),
                tokenizer=tokenizer,
                chunk_size=chunk_tok_size,
                chunk_overlap=subsection_overlap,
                blurb_size=blurb_size,
            )
            chunks.extend(large_section_chunks)
            continue

        # In the case where the whole section is shorter than a chunk, either adding to chunk or start a new one
        if (
            current_tok_length
            + len(tokenizer.tokenize(SECTION_SEPARATOR))
            + section_tok_length
            <= chunk_tok_size
        ):
            chunk_text += (
                SECTION_SEPARATOR + section.text if chunk_text else section.text
            )
            link_offsets[curr_offset_len] = section.link
        else:
            chunks.append(
                DocAwareChunk(
                    source_document=document,
                    chunk_id=len(chunks),
                    blurb=extract_blurb(chunk_text, blurb_size),
                    content=chunk_text,
                    source_links=link_offsets,
                    section_continuation=False,
                )
            )
            link_offsets = {0: section.link}
            chunk_text = section.text

    # Once we hit the end, if we're still in the process of building a chunk, add what we have
    if chunk_text:
        chunks.append(
            DocAwareChunk(
                source_document=document,
                chunk_id=len(chunks),
                blurb=extract_blurb(chunk_text, blurb_size),
                content=chunk_text,
                source_links=link_offsets,
                section_continuation=False,
            )
        )
    return chunks


def split_chunk_text_into_mini_chunks(
    chunk_text: str, mini_chunk_size: int = MINI_CHUNK_SIZE
) -> list[str]:
    token_count_func = get_default_tokenizer().tokenize
    sentence_aware_splitter = SentenceSplitter(
        tokenizer=token_count_func, chunk_size=mini_chunk_size, chunk_overlap=0
    )

    return sentence_aware_splitter.split_text(chunk_text)


def split_string(s: str, chunk_size: int = 1024) -> str:
    return [s[i : i + chunk_size] for i in range(0, len(s), chunk_size)]


def chunk_document_FAST(
    document: Document,
    chunk_tok_size: int = CHUNK_SIZE,
    subsection_overlap: int = CHUNK_OVERLAP,
    blurb_size: int = BLURB_SIZE,
) -> list[DocAwareChunk]:
    chunks: list[DocAwareChunk] = []
    for section in document.sections:
        for ind, text in enumerate(split_string(section.text, 1024)):
            chunks.append(
                DocAwareChunk(
                    source_document=document,
                    chunk_id=len(chunks),
                    blurb="",
                    content=text,
                    source_links={0: section.link},
                    section_continuation=ind == 0,
                )
            )
    return chunks


class Chunker:
    @abc.abstractmethod
    def chunk(self, document: Document) -> list[DocAwareChunk]:
        raise NotImplementedError


class DefaultChunker(Chunker):
    def chunk(self, document: Document) -> list[DocAwareChunk]:
        return chunk_document_FAST(document)
