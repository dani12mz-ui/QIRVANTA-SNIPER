from hashlib import sha256
from pathlib import Path
from subprocess import check_output

BASE = "5528556584910faa75f1b76d2d96f14d3852e6e7"
PATH = "research/final_htf_data_integrity/QIRVANTA_GOLD_PRO_HTF_DISPLAY_FIX.pine"
EXPECTED_SHA256 = "a9955a1ed3f402f1b8674b3ef2fabfc3d6578bb20f61d8a7d12ade5f527353a2"
raw = check_output(["git", "show", f"{BASE}:{PATH}"])
if sha256(raw).hexdigest() != EXPECTED_SHA256:
    raise ValueError("Unexpected baseline source")
lines = raw.decode().splitlines(keepends=True)
delete = set()
def remove(*ranges):
    for start, end in ranges:
        delete.update(range(start, end + 1))

# Non-S5 detector bodies, isolated state, and their disconnected dependencies.
remove((9, 9), (11, 12), (121, 129), (386, 427), (430, 482), (606, 606),
       (617, 623), (741, 773), (776, 978), (985, 1008), (1020, 1022), (1027, 1027),
       (1029, 1029), (1032, 1037), (1081, 1081), (1093, 1096), (1103, 1144),
       (1153, 1170), (1185, 1185), (1190, 1208), (1210, 1216), (1219, 1219),
       (1223, 1295))
# Strategy-only management and obsolete alert serialization. RC2 still uses quantity grid.
remove((1542, 1565), (1569, 1727), (605, 605), (680, 680), (1507, 1507), (1527, 1527))
# Disconnected inputs/state/formatters. The retained range, momentum, FVG
# predicates, structure, pivot availability, session times and quality are live.
remove((20, 20), (22, 26), (29, 29), (31, 33), (36, 41), (45, 47),
       (58, 58), (100, 120), (136, 136), (162, 163), (184, 184), (186, 186),
       (190, 191), (194, 195), (203, 215), (219, 219), (221, 221), (224, 224),
       (227, 227), (229, 235), (256, 287), (304, 304), (344, 344), (352, 352),
       (364, 366), (378, 383), (604, 604), (607, 607), (610, 610), (682, 691))
# Wick's private, unused HTF result and permanently disabled logger.
# Preserve the visible Wick observation, lifecycle, IDs and rendering.
remove((1831, 1831), (1836, 1836), (1838, 1866), (1890, 1901), (1919, 1921),
       (1925, 1927), (1931, 1945), (1951, 1951), (1986, 1986), (1993, 1993), (2003, 2003),
       (2011, 2011), (2035, 2035), (2039, 2041), (2044, 2046))
# Keep all book slots and the original step/gate/queue ordering. M2, RAW and
# FILTERED are still dependencies of the S5 evidence and ledger.
edits = {
    6: "// Compile-rescue research derivative of verified UNIFIED 5528556.\n",
    14: "// The only executable signal family is the frozen S5.\n",
    18: "// BEGIN S5 SPECIALIZATION of canonical CORE 0d12e94769569a5af6c70061ae1ea4ff5cf4b41b1d8e0867abf739c70710c1d6\n",
    982: "// S5 RECLAIM state is evaluated on the same confirmed bars as UNIFIED.\n",
    983: "",
    984: "// The legacy family index 2 remains part of event IDs and paper evidence.\n",
    1084: "    int ageLimit = 3\n",
    1097: lines[1097 - 1].replace("else if", "if", 1),
    1209: "if processBar\n",
    1541: "// END S5 SPECIALIZATION\n",
}
for n in (*range(1145, 1153), *range(1220, 1223)):
    edits[n] = lines[n - 1][4:]

def assemble() -> str:
    return "".join(edits.get(i, line) for i, line in enumerate(lines, 1) if i not in delete)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Reproduce the guarded S5-only research compiler input.")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    candidate = assemble().encode()
    digest = sha256(candidate).hexdigest()
    if digest != "96c06840e856a079ed6a45840967ee0e1ec25ed5a9f86cf24bf83b9b9cb7de4a":
        raise ValueError("Reproduction differs from the submitted native input")
    args.output.write_bytes(candidate)
    print(digest)
