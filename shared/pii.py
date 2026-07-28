"""
검색 결과(특히 VOC 이력)에 섞여 있는 개인·조직 식별 정보를 자리표시자로 바꾼다.

왜 코드에서 하나: VOC는 실제 문의 원문이라 계정·이메일·이름·부서가 그대로 들어 있다.
LLM에게 "쓰지 마라"고만 하면 원문이 프롬프트에 들어간 뒤라 유출 경로가 남는다.
그래서 **MCP가 결과를 돌려주기 전에** 먼저 지운다(에이전트는 마스킹된 텍스트만 본다).

한계: 외국 이름이나 흔치 않은 표기까지 정규식으로 다 잡을 수는 없다. 그래서 지시문에도
"식별 정보는 자리표시자로 바꿔 쓴다"는 규칙을 함께 둔다(코드가 1차, 지시문이 2차 방어).
과잉 마스킹(멀쩡한 단어를 지워 문맥이 깨지는 것)이 더 나쁘므로 패턴은 보수적으로 잡았다.
"""
import re

USER_ID = "{사용자 id}"
USER_NAME = "{사용자 이름}"

# 조직 접미사 -> 자리표시자. 긴 접미사부터 매칭해야 "사업부"가 "부"로 잘리지 않는다.
_ORG_SUFFIXES = ["사업부", "본부", "부문", "센터", "그룹", "파트", "모듈", "부서", "팀"]
_ORG_RE = re.compile(
    r"[A-Za-z가-힣0-9][A-Za-z가-힣0-9\s]{0,15}?(" + "|".join(_ORG_SUFFIXES) + r")\b")

# 이메일 전체를 계정 자리표시자로 바꾼다(도메인도 조직 식별 정보라 남기지 않는다).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 사내 계정 형태(영문+숫자 뒤에 점, 예: yr9.choi). 파일명(server.log)과 헷갈리지 않도록
# '점 앞에 숫자가 있는' 형태만 잡는다.
_ACCOUNT_RE = re.compile(r"\b[A-Za-z]{1,4}[0-9]{1,4}\.[A-Za-z][A-Za-z0-9_-]{1,20}\b")

# 한국 성씨(빈도순 상위). 성+이름 2~3자가 하나의 토큰으로 붙어 있을 때만 이름으로 본다.
_SURNAMES = (
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구"
    "라마원방변석선설성소신엄여염오용우육윤인장전정제조진차천최추탁태편표피하한함현형홍황"
)
# 뒤에 오는 호칭/직급으로 이름임을 확인한다. 한국어는 조사가 붙으므로("책임이 요청") 단어
# 경계(\b)를 쓰면 매칭되지 않는다 - 호칭 자체만 확인한다.
_NAME_RE = re.compile(rf"(?<![가-힣])[{_SURNAMES}][가-힣]{{1,2}}(?=\s*(님|씨|책임|선임|수석|프로|"
                      r"사원|주임|대리|과장|차장|부장|팀장|파트장|그룹장|매니저|연구원))")
# 직급 없이 단독으로 쓰인 이름은 오탐이 크므로, '담당자: 홍길동'처럼 라벨이 붙은 경우만 잡는다.
# 이름 뒤 공백까지 먹어 문장이 붙지 않도록 토큰 단위로만 잡는다("예림 최", "John Smith" 포함).
_LABELED_NAME_RE = re.compile(
    r"(담당자|요청자|의뢰자|작성자|신청자|문의자|처리자|승인자|성명|이름)\s*[:：]\s*"
    r"[A-Za-z가-힣]{1,12}(?:\s+[A-Za-z가-힣]{1,12})?")


def _org_repl(m: re.Match) -> str:
    return "{" + m.group(1) + "명}"


def mask_pii(text: str | None) -> str | None:
    """사람·조직 식별 정보를 자리표시자로 바꾼 문자열을 돌려준다(None은 그대로)."""
    if not text:
        return text
    out = _EMAIL_RE.sub(USER_ID, text)
    out = _ACCOUNT_RE.sub(USER_ID, out)
    out = _LABELED_NAME_RE.sub(lambda m: f"{m.group(1)}: {USER_NAME}", out)
    out = _NAME_RE.sub(USER_NAME, out)
    out = _ORG_RE.sub(_org_repl, out)
    return out


def mask_record(record: dict, fields: tuple[str, ...]) -> dict:
    """dict의 지정 필드만 마스킹한 새 dict를 돌려준다."""
    masked = dict(record)
    for f in fields:
        if isinstance(masked.get(f), str):
            masked[f] = mask_pii(masked[f])
    return masked
