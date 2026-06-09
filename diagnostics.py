# -*- coding: utf-8 -*-
import traceback

def classify_error(exc):
    msg = str(exc)
    name = exc.__class__.__name__
    lower = msg.lower()
    if "داده کافی" in msg or "ohlcv" in lower or "candle" in lower:
        return "DATA_NOT_ENOUGH", "داده کندل کافی نیست یا API دیتای کامل نداده"
    if "too many requests" in lower or "429" in msg:
        return "API_RATE_LIMIT", "محدودیت درخواست API یا 429"
    if "timeout" in lower or "timed out" in lower:
        return "API_TIMEOUT", "تاخیر یا قطع ارتباط با API"
    if "does not have market symbol" in lower or "نماد" in msg:
        return "SYMBOL_NOT_SUPPORTED", "نماد در صرافی پشتیبانی نمی‌شود"
    if "json" in lower or "decode" in lower:
        return "JSON_ERROR", "خطا در خواندن/نوشتن فایل JSON"
    if name in ["KeyError", "IndexError"]:
        return "DATA_FIELD_ERROR", "یکی از فیلدهای موردنیاز در داده وجود ندارد"
    if name in ["TypeError", "ValueError"]:
        return "VALUE_ERROR", "نوع یا مقدار داده نامعتبر است"
    return name, "خطای عمومی یا ناشناخته"

def format_error_report(section, exc, file_name=None, function_name=None, symbol=None):
    code, cause = classify_error(exc)
    lines = [f"❌ خطا در بخش: {section}", f"نوع خطا: {code}", f"علت احتمالی: {cause}"]
    if file_name:
        lines.append(f"فایل احتمالی: {file_name}")
    if function_name:
        lines.append(f"تابع: {function_name}")
    if symbol:
        lines.append(f"نماد: {symbol}")
    lines.append(f"جزئیات: {str(exc)[:500]}")
    return "\n".join(lines)

def log_exception(section, exc, file_name=None, function_name=None, symbol=None):
    report = format_error_report(section, exc, file_name, function_name, symbol)
    print(report)
    print(traceback.format_exc())
    return report
