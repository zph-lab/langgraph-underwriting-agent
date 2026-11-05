"""上传文件 → 纯文本提取（Web 附件 · 方案 B 纯内联，原始字节不落盘）。

对外只暴露 extract_text(filename, data) -> (text, truncated)；
一切面向用户的失败（类型不支持/扫描件/超限文案/依赖缺失）统一抛 ExtractError(友好文案)。
"""
from __future__ import annotations

import io

MAX_BYTES = 20 * 1024 * 1024   # 单文件原始大小上限（20MB）
MAX_CHARS = 30_000             # 提取纯文本上限（超出截断，多文件各自独立计）

TEXT_EXTS = {
    "txt", "md", "markdown", "json", "csv", "tsv", "log", "xml", "yaml", "yml",
    "ini", "conf", "properties", "sql", "py", "js", "ts", "java", "c", "h",
    "cpp", "hpp", "go", "rs", "sh", "bat", "ps1", "html", "css",
}
SUPPORTED_EXTS = TEXT_EXTS | {"pdf", "docx"}


class ExtractError(RuntimeError):
    """可直接展示给用户的友好错误。"""


def _ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ExtractError("服务端缺少 pypdf，请执行 pip install pypdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractError(f"PDF 无法解析：{type(exc).__name__}") from exc
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except Exception as exc:
            raise ExtractError(f"PDF 解密失败：{type(exc).__name__}") from exc
        if not ok:
            raise ExtractError("PDF 已加密，请去除密码后再上传")
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n".join(pages).strip()
    if not text:
        raise ExtractError("该 PDF 提取不到文字（可能为扫描件或纯图片），暂不支持 OCR")
    return text


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise ExtractError("服务端缺少 python-docx，请执行 pip install python-docx") from exc
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractError(
            f"docx 无法解析（老版 .doc 不支持，请另存为 .docx 后重试）：{type(exc).__name__}"
        ) from exc
    parts: list[str] = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ExtractError("文档没有可提取的文字内容")
    return text


def extract_text(filename: str, data: bytes) -> tuple[str, bool]:
    """按扩展名提取纯文本。返回 (text, truncated)。"""
    ext = _ext_of(filename or "")
    if ext == "doc":
        raise ExtractError("暂不支持 Word 97-2003 的 .doc，请另存为 .docx 后上传")
    if ext not in SUPPORTED_EXTS:
        raise ExtractError(f"不支持的文件类型：.{ext or '未知'}")
    if not data:
        raise ExtractError("文件为空")
    if ext == "pdf":
        text = _extract_pdf(data)
    elif ext == "docx":
        text = _extract_docx(data)
    else:
        text = _decode_text(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]
    return text, truncated
