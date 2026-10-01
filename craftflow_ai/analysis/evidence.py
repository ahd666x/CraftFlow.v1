"""
موتور شواهد، یافته و اطمینان (فاز ۲).

اصل حاکم: **LLM هیچ‌کدام از اعداد اینجا را نمی‌سازد.** هر چیزی که به کاربر
نشان داده می‌شود باید از یک ``Evidence`` با ``source`` و ``query`` مشخص آمده
باشد. Finding را هم فقط همین ماژول می‌سازد؛ مدل زبانی حق ساختن Finding ندارد.

قرارداد ``query``:
  * هرگز SQL نیست.
  * توصیف انسانی/ماشینی predicate است، مثل ``"tasks of this order grouped by status"``.

قرارداد ``value``:
  * فقط مقدار خام و قابل پردازش ماشینی.
  * هرگز متن نمایشی فارسی داخل ``value`` نیست (متن در ``Finding.summary`` است).
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from craftflow_ai.analysis.limits import (
    STATUS_INSUFFICIENT_DATA,
    STATUS_UNKNOWN,
    limitation_codes,
    limitation_messages,
    limitations_payload,
)

# ----------------------------------------------------------------------
# سطوح اطمینان — فقط همین سه، و فقط سمت سرور تعیین می‌شود
# ----------------------------------------------------------------------

CONFIDENCE_HIGH = 'high'
CONFIDENCE_MEDIUM = 'medium'
CONFIDENCE_LOW = 'low'

CONFIDENCE_LEVELS = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)

# سیگنال‌های ورودی موتور اطمینان
SIGNAL_REAL = 'real'          # دادهٔ واقعی و به‌اندازهٔ کافی موجود
SIGNAL_DERIVED = 'derived'    # دادهٔ مشتق‌شده یا ناقص
SIGNAL_MISSING = 'missing'    # منبع تصمیم وجود ندارد


@dataclass(frozen=True)
class Evidence:
    """یک شواهد عددی/واقعیتی با منبع و توصیف دقیق predicate."""

    metric: str
    value: Any
    unit: str
    source: str
    query: str
    comparison: str | None = None
    threshold: str | None = None
    derived: bool = False

    def __post_init__(self):
        if not self.metric or not str(self.metric).strip():
            raise ValueError('Evidence.metric must be a non-empty string')
        if not self.source or not str(self.source).strip():
            raise ValueError('Evidence.source is mandatory')
        if not self.query or not str(self.query).strip():
            raise ValueError('Evidence.query is mandatory')
        if not self.unit or not str(self.unit).strip():
            raise ValueError('Evidence.unit must be a non-empty string')
        object.__setattr__(self, 'metric', str(self.metric).strip())
        object.__setattr__(self, 'source', str(self.source).strip())
        object.__setattr__(self, 'query', str(self.query).strip())
        object.__setattr__(self, 'unit', str(self.unit).strip())

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            'metric': self.metric,
            'value': self.value,
            'unit': self.unit,
            'source': self.source,
            'query': self.query,
        }
        if self.comparison is not None:
            payload['comparison'] = self.comparison
        if self.threshold is not None:
            payload['threshold'] = self.threshold
        if self.derived:
            payload['derived'] = True
        return payload


# ----------------------------------------------------------------------
# موتور اطمینان
# ----------------------------------------------------------------------

# ترتیب شدت: بدترین سیگنال تعیین‌کننده است. نبودِ منبع، از مشتق‌بودن بدتر است.
_SIGNAL_PRECEDENCE = (SIGNAL_MISSING, SIGNAL_DERIVED, SIGNAL_REAL)


def normalize_signal(value: Any) -> str:
    """نگاشت هر ورودی به یکی از سه سیگنال مجاز."""
    if isinstance(value, bool):
        return SIGNAL_REAL if value else SIGNAL_MISSING
    if value is None:
        return SIGNAL_MISSING
    text = str(value).strip().lower()
    if text in CONFIDENCE_LEVELS:
        # اگر کسی سطح اطمینان نهایی داد، به سیگنال تبدیل می‌شود.
        return {
            CONFIDENCE_HIGH: SIGNAL_REAL,
            CONFIDENCE_MEDIUM: SIGNAL_DERIVED,
            CONFIDENCE_LOW: SIGNAL_MISSING,
        }[text]
    if text in ('unknown', 'insufficient_data', 'not_computable', 'missing', 'empty'):
        return SIGNAL_MISSING
    if text in ('derived', 'partial', 'approximate', 'estimated', 'proxy'):
        return SIGNAL_DERIVED
    return SIGNAL_REAL


def confidence_from_signals(signals: Sequence[Any]) -> str:
    """
    اطمینان قطعی از ترکیب سیگنال‌های تصمیم.

    high   → همهٔ ورودی‌های تصمیم‌کننده واقعی و کافی هستند.
    medium → دست‌کم یک دامنهٔ تصمیم‌کننده مشتق‌شده یا ناقص است.
    low    → منبع تصمیم وجود ندارد.
    """
    resolved = [normalize_signal(s) for s in (signals or ())]
    resolved = [s for s in resolved if s]
    if not resolved:
        return CONFIDENCE_LOW
    if SIGNAL_MISSING in resolved:
        return CONFIDENCE_LOW
    if SIGNAL_DERIVED in resolved:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_HIGH


def worst_confidence(*levels: str) -> str:
    """بدترین (پایین‌ترین) سطح اطمینان — برای تجمیع چند دامنه."""
    rank = {CONFIDENCE_LOW: 0, CONFIDENCE_MEDIUM: 1, CONFIDENCE_HIGH: 2}
    picked = [rank.get(str(level or '').lower(), 0) for level in levels if level]
    if not picked:
        return CONFIDENCE_LOW
    for level, value in rank.items():
        if value == min(picked):
            return level
    return CONFIDENCE_LOW


# ----------------------------------------------------------------------
# یافته
# ----------------------------------------------------------------------

# ترتیب شدت یافته — «بدترین» یافته تعیین‌کنندهٔ وضعیت کلی است.
SEVERITY_ORDER = ('critical', 'high', 'medium', 'low', 'info')

_DOMAIN_STATUSES = frozenset({
    'healthy', 'warning', 'stalled', 'blocked',
    'in_progress', 'complete', 'not_started', 'unknown', 'insufficient_data',
})


@dataclass(frozen=True)
class Finding:
    """یک یافتهٔ قطعی؛ فقط سمت سرور ساخته می‌شود، نه توسط LLM."""

    finding_id: str
    domain: str
    severity: str
    summary: str
    evidence: Tuple[Evidence, ...] = ()
    confidence: str = CONFIDENCE_LOW
    limitations: Tuple[str, ...] = ()
    derived: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'evidence', tuple(self.evidence or ()))
        object.__setattr__(self, 'limitations', limitation_codes(self.limitations))
        object.__setattr__(self, 'confidence', str(self.confidence or CONFIDENCE_LOW).lower())
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f'confidence must be one of {CONFIDENCE_LEVELS}')
        if not self.finding_id or not str(self.finding_id).strip():
            raise ValueError('Finding.finding_id must be a non-empty string')
        if not self.domain or not str(self.domain).strip():
            raise ValueError('Finding.domain must be a non-empty string')
        if not self.severity or not str(self.severity).strip():
            raise ValueError('Finding.severity must be a non-empty string')
        if not self.evidence:
            raise ValueError(f'Finding {self.finding_id} must carry at least one evidence')
        if self.derived and not self.limitations:
            raise ValueError(
                f'Finding {self.finding_id} is derived and must declare a limitation'
            )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            'finding_id': self.finding_id,
            'domain': self.domain,
            'severity': self.severity,
            'summary': self.summary,
            'evidence': [e.to_dict() for e in self.evidence],
            'confidence': self.confidence,
            'limitations': list(self.limitations),
        }
        if self.derived:
            payload['derived'] = True
        if self.detail:
            payload['detail'] = self.detail
        return payload


def worst_severity(*severities: str) -> str:
    picked = [s for s in severities if s]
    if not picked:
        return 'info'
    for level in SEVERITY_ORDER:
        if level in picked:
            return level
    return 'info'


# ----------------------------------------------------------------------
# دامنهٔ تحلیل (مثلاً production / materials / quality)
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class DomainResult:
    """نتیجهٔ یک دامنه؛ همیشه وضعیت صریح دارد، حتی وقتی داده نیست."""

    domain: str
    status: str
    confidence: str
    summary: str
    evidence: Tuple[Evidence, ...] = ()
    limitations: Tuple[str, ...] = ()
    findings: Tuple[Finding, ...] = ()
    confidence_reasons: Tuple[str, ...] = ()
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'evidence', tuple(self.evidence or ()))
        object.__setattr__(self, 'findings', tuple(self.findings or ()))
        object.__setattr__(self, 'confidence_reasons', tuple(self.confidence_reasons or ()))
        object.__setattr__(self, 'limitations', limitation_codes(self.limitations))
        object.__setattr__(self, 'confidence', str(self.confidence or CONFIDENCE_LOW).lower())
        status = str(self.status or '').strip()
        if status not in _DOMAIN_STATUSES:
            raise ValueError(
                f'Domain {self.domain}: unknown status {status!r}; '
                f'allowed: {sorted(_DOMAIN_STATUSES)}'
            )
        object.__setattr__(self, 'status', status)
        if status in ('unknown', 'insufficient_data') and not self.limitations:
            raise ValueError(
                f'Domain {self.domain} reports {status} and must declare a limitation'
            )

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            'domain': self.domain,
            'status': self.status,
            'confidence': self.confidence,
            'confidence_reasons': list(self.confidence_reasons),
            'summary': self.summary,
            'evidence': [e.to_dict() for e in self.evidence],
            'limitations': list(self.limitations),
            'limitations_detail': limitations_payload(self.limitations),
            'findings': [f.to_dict() for f in self.findings],
        }
        if self.detail:
            payload['detail'] = self.detail
        return payload


# ترتیب تعیین وضعیت کلی: بدترین دامنه تصمیم می‌گیرد.
# «unknown» عمداً پایین‌ترین رتبه است تا وضعیت بدترِ واقعی را پنهان نکند؛
# دامنه‌های نامعلوم جداگانه در ``unknown_domains`` گزارش می‌شوند.
OVERALL_STATUS_PRECEDENCE = (
    'blocked', 'stalled', 'warning', 'in_progress',
    'not_started', 'unknown', 'healthy',
)

STATUS_COMPLETE = 'complete'


def rollup_status(statuses: Sequence[str]) -> str:
    """
    وضعیت کلی از روی وضعیت دامنه‌ها.

    ``complete`` فقط وقتی برگردانده می‌شود که *همهٔ* دامنه‌ها کامل باشند؛ در غیر
    این صورت بدترین دامنهٔ باقی‌مانده تعیین‌کننده است.
    """
    present = {s for s in statuses if s}
    if not present:
        return STATUS_UNKNOWN
    if present == {STATUS_COMPLETE}:
        return STATUS_COMPLETE
    for candidate in OVERALL_STATUS_PRECEDENCE:
        if candidate in present:
            return candidate
    return STATUS_UNKNOWN


# ----------------------------------------------------------------------
# گزارش تحلیل
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class AnalysisReport:
    """خروجی قطعی یک تحلیل؛ قالب JSON پیشنهادی بخش ۲۷ پروتکل."""

    status: str
    confidence: str
    findings: Tuple[Finding, ...] = ()
    limitations: Tuple[str, ...] = ()
    confidence_reasons: Tuple[str, ...] = ()
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'findings', tuple(self.findings or ()))
        object.__setattr__(self, 'limitations', limitation_codes(self.limitations))
        object.__setattr__(self, 'confidence_reasons', tuple(self.confidence_reasons or ()))
        object.__setattr__(self, 'confidence', str(self.confidence or CONFIDENCE_LOW).lower())

    def analysis_payload(self) -> Dict[str, Any]:
        """
        بدنهٔ ``analysis``؛ دقیقاً همان ساختار توصیه‌شدهٔ بخش ۲۷.

        دادهٔ ناکافی هرگز به صفر تبدیل نمی‌شود — ``status`` می‌شود
        ``insufficient_data`` یا ``unknown`` و ``findings`` خالی می‌ماند.
        """
        return {
            'status': self.status,
            'confidence': self.confidence,
            'confidence_reasons': list(self.confidence_reasons),
            'findings': [f.to_dict() for f in self.findings],
            'limitations': list(self.limitations),
            'limitations_detail': limitations_payload(self.limitations),
        }

    def to_payload(self, **extra) -> Dict[str, Any]:
        payload: Dict[str, Any] = {'analysis': self.analysis_payload()}
        payload.update(extra)
        return payload


def insufficient_data(reason: str, *, confidence_reasons: Sequence[str] = (),
                       detail: Dict[str, Any] | None = None) -> AnalysisReport:
    """سازندهٔ گزارش «داده کافی نیست» — بدون هیچ عدد ساختگی."""
    return AnalysisReport(
        status='insufficient_data',
        confidence=CONFIDENCE_LOW,
        findings=(),
        limitations=(reason,),
        confidence_reasons=tuple(confidence_reasons) or (
            f'منبع تصمیم «{reason}» در دادهٔ فعلی موجود نیست.',
        ),
        detail=dict(detail or {}),
    )


def unavailable_reason(status: str, reason: str) -> str:
    return reason if status in ('unknown', 'insufficient_data') else status


__all__ = [
    'AnalysisReport',
    'CONFIDENCE_HIGH',
    'CONFIDENCE_LEVELS',
    'CONFIDENCE_LOW',
    'CONFIDENCE_MEDIUM',
    'DomainResult',
    'Evidence',
    'Finding',
    'OVERALL_STATUS_PRECEDENCE',
    'SEVERITY_ORDER',
    'SIGNAL_DERIVED',
    'SIGNAL_MISSING',
    'SIGNAL_REAL',
    'confidence_from_signals',
    'insufficient_data',
    'limitation_messages',
    'normalize_signal',
    'rollup_status',
    'worst_confidence',
    'worst_severity',
]