"""Make a disposable native Pine buffer from the unchanged V2 Intelligence release."""
import argparse
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "unified/experimental/QIRVANTA_SNIPER_V2_INTELLIGENCE.pine"
PIN = "4b4bfc9faeb61082596d52052b6a05d884ca66c95b9aaad3c843edeb4346642f"
START = 1794182400000  # 2026-11-09 00:00 UTC
STOP = 1794355200000   # 2026-11-11 00:00 UTC


def build():
    raw = SOURCE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PIN:
        raise ValueError("V2 Intelligence source does not match the baseline")
    text = raw.decode()
    for old, new in (
        ('input.bool(false, "Pine Logs: candidates', 'input.bool(true, "Pine Logs: candidates'),
        ('input.time(0, "Log from"', f'input.time({START}, "Log from"'),
        ('input.time(4102444800000, "Log until"', f'input.time({STOP}, "Log until"'),
    ):
        if text.count(old) != 1:
            raise ValueError(f"Probe input anchor changed: {old}")
        text = text.replace(old, new)
    rows = [
        "time", "time_close", "open", "high", "low", "close", "priorATR", "adx", "ema21", "ema55",
        "momentum", "atrRatio", "acceleration", "h1Bias", "h4Bias", "h1TimeRaw", "h4TimeRaw",
        "displacementUp", "displacementDown", "exhaustion", "candidateSide", "candidateGrade",
        "swingHigh", "swingLow", "pendingSide",
    ]
    formats = {"open", "high", "low", "close", "swingHigh", "swingLow"}
    expr = ' + "|" + '.join(f"str.tostring({col}, format.mintick)" if col in formats else f"str.tostring({col})" for col in rows)
    text += f"""
// Disposable forensic tap. No signal conditions or source artifact are changed.
if barstate.isconfirmed and time >= {START} and time_close <= {STOP}
    log.info("V21_INTEL_BAR|" + timeframe.period + "|" + {expr} + "|" + lastStructure + "|" + warning)
"""
    return text.encode()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    source = build()
    with args.output.open("xb") as f:
        f.write(source)
    print(hashlib.sha256(source).hexdigest(), len(source))


if __name__ == "__main__":
    main()
