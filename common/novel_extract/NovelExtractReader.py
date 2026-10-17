"""按章节/段落分块，保留原文位置和前块上下文；不按块推断故事时间。"""
from dataclasses import asdict, dataclass
import re

HEADING = re.compile(r'(?im)^[ \t]*(?:(?:act|scene|part|chapter|book|section|stave|canto|volume|episode)\s+\S+|第[一二三四五六七八九十百千万零〇0-9]+[章节部卷幕回]).*$')


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int
    end: int
    text: str
    previous_context: str

    def payload(self):
        return asdict(self)

    def passages(self):
        """可引用的原文片段；编号只表示来源，不表示事件或故事时间。"""
        return {f'C{self.index}_P{i}': match.group() for i, match in enumerate(
            re.finditer(r'[^\n]+(?:\n|$)', self.text), 1) if match.group().strip()}

    def context(self, chunks=()):
        previous = chunks[self.index - 2].passages() if chunks and self.index > 1 else {}
        return {'index': self.index, 'passages': self.passages(),
                'previous_context': self.previous_context,
                'previous_passages': {key: quote for key, quote in previous.items()
                                      if quote.strip() in self.previous_context}}


def compact_evidence(value, chunks):
    """上下文已含完整原文；重复的整段证据用唯一片段编号表达，不改写持久资料。"""
    references = {}
    for chunk in chunks:
        for key, quote in chunk.passages().items():
            identity = (chunk.index, quote.strip())
            references[identity] = key if identity not in references else None
    def visit(item):
        if isinstance(item, list):
            return [visit(row) for row in item]
        if not isinstance(item, dict):
            return item
        if isinstance(item.get('quote'), str) and isinstance(item.get('chunk'), int):
            key = references.get((item['chunk'], item['quote'].strip()))
            if key and not item.get('passage_ids'):
                return {**item, 'quote': '', 'passage_ids': [key]}
        return {key: visit(row) for key, row in item.items()}
    return visit(value)


def full_source_context(chunks):
    """全文校验保留全部可引用片段，但不重复发送块间重叠上下文。"""
    return [{'index': chunk.index, 'passages': chunk.passages()} for chunk in chunks]


def chunk_text(text, min_chars=800, max_chars=1200, overlap=300):
    """参考 Shadow-Loom 的章节优先、段落回退；自实现精确保留字符。"""
    if not 0 < min_chars <= max_chars or overlap < 0:
        raise ValueError('分块参数非法')
    headings = [match.start() for match in HEADING.finditer(text)]
    boundaries = sorted(set([0, *headings, len(text)]))
    chunks = []
    for start, end in zip(boundaries, boundaries[1:]):
        while start < end:
            stop = min(start + max_chars, end)
            if stop < end:
                # 优先在段落末尾，其次句末/空格；长无标点文本按字符安全切开。
                window = text[start + min_chars:stop]
                splits = list(re.finditer(r'\n\s*\n', window)) or list(re.finditer(r'[。！？!?；;]\s*|\.\s+|\s+', window))
                if splits:
                    stop = start + min_chars + splits[-1].end()
            body = text[start:stop]
            chunks.append(Chunk(len(chunks) + 1, start, stop, body, text[max(0, start-overlap):start]))
            start = stop
    return chunks
