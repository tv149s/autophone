"""号码归一化到 E.164,匹配/去重都用归一化后的号码。"""
import phonenumbers

DEFAULT_REGION = "US"


def normalize(raw: str, region: str = DEFAULT_REGION) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    try:
        n = phonenumbers.parse(raw, None if raw.startswith("+") else region)
        if phonenumbers.is_possible_number(n):
            return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        pass
    # 兜底:只留数字和开头的 +
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 10 and region == "US":
        return "+1" + digits
    return "+" + digits
