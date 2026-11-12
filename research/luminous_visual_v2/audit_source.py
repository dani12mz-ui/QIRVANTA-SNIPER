import argparse
import hashlib
import json
from pathlib import Path
from subprocess import check_output
from research.s5_compile_rescue.static_graph import inventory

parser = argparse.ArgumentParser(description="Verify the Visual V2 source against the protected 26ad1aa Pine.")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

ref="26ad1aa2992f9e2e7e28c13fbbd3f321e1de8261"
old=check_output(["git","show",f"{ref}:research/s5_compile_rescue/QIRVANTA_GOLD_PRO_S5_LUMINOUS.pine"])
new=Path("research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine").read_bytes()
digest=lambda raw:hashlib.sha256(raw).hexdigest()
assert digest(old)=="023c9eaf8a0e0085fd187b4bf9b5f76eb48017dd187c5e5b04cb8e542ff852ce"
boundary=b"// Presentation reads the frozen decision"
assert old.split(boundary,1)[0].splitlines()[7:]==new.split(boundary,1)[0].splitlines()[7:]
def part(source,a,b):
    return source.split(a,1)[1].split(b,1)[0]
for a,b in [(b"if lzShow and rcViewPaintBar",b"var array<box> lzBands"),
            (b"bool rcAlertWatch","// ═══ LUMINOUS".encode()),
            (b'        log.info("LZ_EVENT|',b"    if ")]:
    assert part(old,a,b)==part(new,a,b),a
a,ap,_=inventory(old)
b,bp,_=inventory(new)
assert a["protected_call_counts"]==b["protected_call_counts"]
for name in ("label.new","line.new","box.new","table.new"):
    assert b["drawing_call_counts"].get(name)==a["drawing_call_counts"].get(name)
assert ap.keys()==bp.keys()
for name in ap:
    if name=="input.string":
        old_inputs=[["\"Large\"" if i==0 and call[:3]==["\"Standard\"",",","\"Размер на таблото\""] else token for i,token in enumerate(call)] for call in ap[name]]
        assert bp[name]==old_inputs
    else:
        assert ap[name]==bp[name],name
mods=[name for name in a["function_body_digests"] if a["function_body_digests"][name]!=b["function_body_digests"][name]]
assert set(mods)<={"f_rcViewSignal","f_rcViewPlanLevel","f_rcPressureWidget","f_lzEvent"}
report={"status":"STATIC_CHECKS_PASSED","baseline":ref,"baseline_sha256":digest(old),"candidate_sha256":digest(new),"source_bytes":[len(old),len(new)],"source_lines":[a["lines"],b["lines"]],"lexical_tokens":[a["lexical_tokens"],b["lexical_tokens"]],"modified_functions":mods,"protected_calls":b["protected_call_counts"],"constructors":{k:v for k,v in b["drawing_call_counts"].items() if k.endswith(".new")},"object_caps":[60,26,12],"source_only":True}
args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf8",newline="\n")
print(report)
