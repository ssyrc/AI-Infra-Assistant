"""
차트를 **순수 파이썬으로 SVG 문자열**로 그린다. 외부 라이브러리도, 외부 렌더 서버도 쓰지 않는다.

왜 matplotlib이 아닌가(폐쇄망 제약):
  1) 새 pip 패키지는 이미지 재빌드를 부른다(반영 절차 B). SVG는 표준 라이브러리만 쓰므로
     코드만 rsync하고 재시작하면 끝난다(절차 A).
  2) slim 이미지에는 **한글 폰트가 없다.** matplotlib으로 그리면 축 라벨이 전부 두부(□□□)가
     된다. SVG는 글자를 그대로 담고 브라우저 폰트로 그려지므로 한글이 깨지지 않는다.
  3) PNG는 바이트가 커서 결과를 프롬프트에 실을 수 없다. SVG는 파일로 저장하고
     URL만 돌려주면 되므로 컨텍스트 예산에 영향이 없다.

antvis/mcp-server-chart를 그대로 쓰지 않은 이유는 docs/HISTORY.md #110 참고
(기본값이 외부 렌더 서버 호출이라 폐쇄망에서 동작하지 않는다).
"""
import html
import math

# 색맹 사용자도 구분되는 순서로 배치한 팔레트(파랑 -> 주황 -> 청록 -> 자주 ...).
PALETTE = ["#3b6fd4", "#e08b2f", "#2a9d8f", "#a45ba4", "#c2504a",
           "#6d8b3a", "#7a6fd4", "#b07a3f"]

CHART_TYPES = ("line", "bar", "pie", "scatter")

_W, _H = 760, 440
_PAD = {"top": 56, "right": 24, "bottom": 78, "left": 72}

# 다크 모드로 보는 사용자를 위해 두 벌을 담는다. <img>로 참조된 SVG에서도 최신 브라우저는
# prefers-color-scheme를 따르며, 지원하지 않으면 밝은 쪽(기본값)으로 그려져 그대로 읽을 수 있다.
_STYLE = """
  .bg   { fill: #ffffff; }
  .card { stroke: #e2e5ea; fill: none; }
  .grid { stroke: #e8ebf0; }
  .axis { stroke: #9aa3b0; }
  .t    { fill: #2b3440; font-family: system-ui,-apple-system,'Malgun Gothic','Noto Sans KR',sans-serif; }
  .ttl  { font-size: 16px; font-weight: 600; }
  .lbl  { font-size: 11.5px; }
  .mut  { fill: #6b7480; font-size: 11.5px; }
  @media (prefers-color-scheme: dark) {
    .bg   { fill: #1b1f24; }
    .card { stroke: #333a42; }
    .grid { stroke: #2c333b; }
    .axis { stroke: #6b7480; }
    .t    { fill: #e6e9ee; }
    .mut  { fill: #9aa3b0; }
  }
"""


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _fmt(v: float) -> str:
    """축 눈금/값 표기. 정수는 정수로, 큰 수는 k/M로 줄인다."""
    if v is None:
        return ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M".replace(".0M", "M")
    if a >= 10_000:
        return f"{v / 1000:.0f}k"
    if a >= 1000:
        return f"{v / 1000:.1f}k".replace(".0k", "k")
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """1/2/5 x 10^n 규칙으로 눈금을 고른다(0.1, 0.2, 0.5, 1, 2, 5, 10 ...)."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(1, count)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 5, 10):
        step = m * mag
        if raw <= step:
            break
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        # 부동소수 누적 오차로 -0.0이나 2.9999가 나오지 않게 스텝 단위로 반올림한다.
        ticks.append(round(v / step) * step)
        v += step
    return ticks


def _series_values(series: list[dict]) -> list[list]:
    return [list(s.get("values") or []) for s in series]


def _y_range(series: list[dict], include_zero: bool) -> tuple[float, float]:
    flat = [v for vals in _series_values(series) for v in vals if v is not None]
    if not flat:
        return 0.0, 1.0
    lo, hi = min(flat), max(flat)
    if include_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if lo == hi:
        # 값이 전부 같으면 선이 축에 붙어 안 보인다. 위아래로 여유를 준다.
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    return lo, hi


def _legend(series: list[dict], y: int) -> list[str]:
    out, x = [], _PAD["left"]
    for i, s in enumerate(series):
        color = PALETTE[i % len(PALETTE)]
        name = _esc(s.get("name") or f"계열 {i + 1}")
        out.append(f'<rect x="{x}" y="{y - 8}" width="10" height="10" rx="2" fill="{color}"/>')
        out.append(f'<text class="t lbl" x="{x + 15}" y="{y + 1}">{name}</text>')
        x += 26 + len(str(s.get("name") or "")) * 8
    return out


def _x_labels(labels: list[str], x_of, y: int) -> list[str]:
    """x축 라벨. 개수가 많으면 건너뛰며 찍고, 길면 기울여 겹치지 않게 한다."""
    n = len(labels)
    step = max(1, math.ceil(n / 12))
    tilt = n > 6 or any(len(str(t)) > 6 for t in labels)
    out = []
    for i, label in enumerate(labels):
        if i % step:
            continue
        cx, text = x_of(i), _esc(label)
        if tilt:
            out.append(f'<text class="t lbl" x="{cx:.1f}" y="{y}" text-anchor="end" '
                       f'transform="rotate(-35 {cx:.1f} {y})">{text}</text>')
        else:
            out.append(f'<text class="t lbl" x="{cx:.1f}" y="{y}" text-anchor="middle">{text}</text>')
    return out


def _frame(title: str, y_label: str) -> list[str]:
    out = [f'<rect class="bg" x="0" y="0" width="{_W}" height="{_H}" rx="10"/>',
           f'<rect class="card" x="0.5" y="0.5" width="{_W - 1}" height="{_H - 1}" rx="10"/>']
    if title:
        out.append(f'<text class="t ttl" x="{_PAD["left"]}" y="30">{_esc(title)}</text>')
    if y_label:
        # 세로축 단위는 y축 맨 위 눈금 바로 위에 둔다(제목과 붙지 않게).
        out.append(f'<text class="t mut" x="{_PAD["left"]}" y="{_PAD["top"] - 6}">{_esc(y_label)}</text>')
    return out


def _axes(labels, series, include_zero, x_of):
    """격자 + y축 눈금 + x축 라벨을 그리고, 값→화면좌표 변환 함수를 함께 돌려준다."""
    top, bottom = _PAD["top"], _H - _PAD["bottom"]
    lo, hi = _y_range(series, include_zero)
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def y_of(v: float) -> float:
        return bottom - (v - lo) / (hi - lo) * (bottom - top)

    out = []
    for t in ticks:
        y = y_of(t)
        out.append(f'<line class="grid" x1="{_PAD["left"]}" y1="{y:.1f}" '
                   f'x2="{_W - _PAD["right"]}" y2="{y:.1f}"/>')
        out.append(f'<text class="t mut" x="{_PAD["left"] - 8}" y="{y + 4:.1f}" '
                   f'text-anchor="end">{_fmt(t)}</text>')
    out.append(f'<line class="axis" x1="{_PAD["left"]}" y1="{top}" '
               f'x2="{_PAD["left"]}" y2="{bottom}"/>')
    out.append(f'<line class="axis" x1="{_PAD["left"]}" y1="{bottom}" '
               f'x2="{_W - _PAD["right"]}" y2="{bottom}"/>')
    out += _x_labels(labels, x_of, bottom + 20)
    return out, y_of


def _render_line(labels, series, scatter=False) -> list[str]:
    left, right = _PAD["left"], _W - _PAD["right"]
    n = len(labels)
    # 점이 하나뿐이면 나누기 0이 된다. 가운데에 찍는다.
    span = (right - left) if n > 1 else 0

    def x_of(i):
        return left + (span * i / (n - 1) if n > 1 else span / 2 + (right - left) / 2)

    body, y_of = _axes(labels, series, include_zero=False, x_of=x_of)
    for si, s in enumerate(series):
        color = PALETTE[si % len(PALETTE)]
        pts = [(x_of(i), y_of(v)) for i, v in enumerate(s.get("values") or []) if v is not None]
        if not pts:
            continue
        if not scatter and len(pts) > 1:
            d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
                         for i, (x, y) in enumerate(pts))
            body.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2" '
                        'stroke-linejoin="round" stroke-linecap="round"/>')
        # 점 개수가 많으면 마커가 선을 덮어 지저분해진다.
        if scatter or len(pts) <= 30:
            r = 3.5 if scatter else 3
            body += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}"/>' for x, y in pts]
    return body


def _render_bar(labels, series) -> list[str]:
    left, right = _PAD["left"], _W - _PAD["right"]
    n, m = max(1, len(labels)), max(1, len(series))
    slot = (right - left) / n
    gap = slot * 0.22
    bw = max(2.0, (slot - gap) / m)

    def x_of(i):
        return left + slot * (i + 0.5)

    body, y_of = _axes(labels, series, include_zero=True, x_of=x_of)
    zero_y = y_of(0)
    for si, s in enumerate(series):
        color = PALETTE[si % len(PALETTE)]
        for i, v in enumerate(s.get("values") or []):
            if v is None:
                continue
            x = left + slot * i + gap / 2 + bw * si
            y = y_of(v)
            top, height = min(y, zero_y), abs(y - zero_y)
            body.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                        f'height="{max(height, 0.8):.1f}" rx="2" fill="{color}"/>')
    return body


def _render_pie(labels, series) -> list[str]:
    values = [abs(v) for v in (series[0].get("values") or []) if v is not None]
    total = sum(values)
    cx, cy, r = 250, _H / 2 + 8, 130
    if total <= 0:
        return [f'<text class="t mut" x="{_W / 2}" y="{_H / 2}" text-anchor="middle">'
                '표시할 값이 없습니다</text>']

    body, angle = [], -math.pi / 2
    for i, v in enumerate(values):
        color = PALETTE[i % len(PALETTE)]
        sweep = 2 * math.pi * v / total
        x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        angle += sweep
        x2, y2 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        if sweep >= 2 * math.pi - 1e-9:
            # 항목이 하나뿐이면 호(arc)로는 원을 못 그린다(시작=끝).
            body.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
            continue
        large = 1 if sweep > math.pi else 0
        body.append(f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} '
                    f'A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{color}"/>')

    # 범례(이름 · 값 · 비율)를 오른쪽에 세로로.
    lx, ly = 430, _PAD["top"] + 14
    for i, v in enumerate(values):
        name = labels[i] if i < len(labels) else f"항목 {i + 1}"
        color = PALETTE[i % len(PALETTE)]
        body.append(f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" rx="2" fill="{color}"/>')
        body.append(f'<text class="t lbl" x="{lx + 16}" y="{ly}">{_esc(name)}</text>')
        body.append(f'<text class="t mut" x="{_W - _PAD["right"]}" y="{ly}" text-anchor="end">'
                    f'{_fmt(v)} ({v / total * 100:.1f}%)</text>')
        ly += 22
        if ly > _H - _PAD["bottom"] + 40:
            break
    return body


def render(chart_type: str, labels: list, series: list[dict],
           title: str = "", y_label: str = "") -> str:
    """차트 SVG 문자열을 만든다. 입력 검증은 호출부(server.py)에서 이미 끝난 상태를 가정한다."""
    body = _frame(title, y_label)
    if chart_type == "bar":
        body += _render_bar(labels, series)
    elif chart_type == "pie":
        body += _render_pie(labels, series)
    elif chart_type == "scatter":
        body += _render_line(labels, series, scatter=True)
    else:
        body += _render_line(labels, series)

    if chart_type != "pie" and len(series) > 1:
        body += _legend(series, _H - 18)

    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W} {_H}" '
            f'width="{_W}" height="{_H}" role="img">'
            f'<style>{_STYLE}</style>' + "".join(body) + "</svg>")
