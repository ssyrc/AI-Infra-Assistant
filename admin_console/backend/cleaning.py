"""
청크 텍스트 정제(clean) 유틸.
업로드된 문서에서 흔히 섞여 들어오는 HTML 태그, HTML 엔티티, 제어문자,
과도한 공백/빈 줄, 깨진 마크다운 이미지/링크 잡음 등을 제거해 검색·임베딩 품질을 높인다.

정제 강도는 옵션으로 조절한다(관리자가 업로드 시 선택):
- strip_html:      HTML 태그와 엔티티 제거
- collapse_space:  연속 공백/탭/빈 줄 정리
- drop_urls:       마크다운 이미지(![...](...)) 및 노출된 URL 제거
- normalize:       유니코드 정규화 + 제어문자 제거 (항상 권장)
"""
import re
import html
import unicodedata
from dataclasses import dataclass

# <script>/<style> 블록은 내용까지 통째로 제거
_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")            # ![alt](url)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")            # [text](url) -> text
_BARE_URL = re.compile(r"https?://\S+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")     # 탭/개행 제외 제어문자
_MULTISPACE = re.compile(r"[ \t]+")
_MULTINEWLINE = re.compile(r"\n{3,}")


@dataclass
class CleanOptions:
    strip_html: bool = True
    collapse_space: bool = True
    drop_urls: bool = False
    normalize: bool = True


def clean_text(text: str, opts: CleanOptions | None = None) -> str:
    if text is None:
        return ""
    opts = opts or CleanOptions()
    t = text

    if opts.normalize:
        t = unicodedata.normalize("NFKC", t)
        t = _CTRL.sub("", t)
        t = t.replace("\xa0", " ").replace("\u200b", "")  # nbsp, zero-width space

    if opts.strip_html:
        t = _SCRIPT_STYLE.sub(" ", t)
        t = _HTML_TAG.sub(" ", t)
        t = html.unescape(t)  # &amp; &lt; &nbsp; 등 실제 문자로
        t = t.replace("\xa0", " ")  # unescape가 되살린 nbsp 재정리

    # 마크다운 이미지 잡음은 항상 제거, 링크는 항상 표시 텍스트만 남긴다(검색 품질↑)
    t = _MD_IMAGE.sub("", t)
    t = _MD_LINK.sub(r"\1", t)

    if opts.drop_urls:
        t = _BARE_URL.sub("", t)

    if opts.collapse_space:
        t = _MULTISPACE.sub(" ", t)
        t = "\n".join(line.strip() for line in t.split("\n"))
        t = _MULTINEWLINE.sub("\n\n", t)

    return t.strip()


def clean_options_from_dict(d: dict | None) -> CleanOptions:
    if not d:
        return CleanOptions()
    return CleanOptions(
        strip_html=bool(d.get("strip_html", True)),
        collapse_space=bool(d.get("collapse_space", True)),
        drop_urls=bool(d.get("drop_urls", False)),
        normalize=bool(d.get("normalize", True)),
    )
