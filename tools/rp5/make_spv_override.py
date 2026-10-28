"""Workaround for "Actual subgroup operation unsupported in shader stage" on GPUs whose driver allows subgroup
operations in compute only (e.g. Adreno 650: supportedStages=0x20).

Builds files/dm/spv-override/<key>.spv for every non-compute shader in the import pack that uses subgroup ops.
In D2R's pack these are fragment shaders with one SHFL.BFLY helper (OpGroupNonUniformShuffle %float) each:
  OpLoad SubgroupLocalInvocationId   -> quad lane from gl_FragCoord: (y&1)<<1 | (x&1)
  OpGroupNonUniformShuffle %float v i -> v + sx*dFdx(v) if (i^lane)&1, + sy*dFdy(v) if (i^lane)&2
Plain OpDPdx/OpDPdy (not Fine): the engine's capability allowlist rejects DerivativeControl.

Usage: python make_spv_override.py <android/shaders> <output spv-override dir> <dir with spirv-dis/spirv-as/spirv-val>
"""
import array
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

SHADERS, OUT, BIN = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]).resolve()
EXE = ".exe" if sys.platform == "win32" else ""
OUT.mkdir(parents=True, exist_ok=True)

CONSTS = """%zq_u0 = OpConstant %uint 0
%zq_u1 = OpConstant %uint 1
%zq_u2 = OpConstant %uint 2
%zq_f0 = OpConstant %float 0
%zq_f1 = OpConstant %float 1
%zq_fm1 = OpConstant %float -1"""


def tool(name, *args):
    subprocess.run([str(BIN / (name + EXE)), *map(str, args)], check=True)


def needs_override(words):
    i, model, hit = 5, None, False
    while i < len(words):
        n, op = words[i] >> 16, words[i] & 0xFFFF
        if n == 0:
            break
        if op == 15 and model is None:
            model = words[i + 1]
        hit |= 333 <= op <= 366
        i += n
    return hit and model != 5  # 5 = GLCompute


def lane(p, fc):
    return [f"%{p}fc = OpLoad %v4float {fc}",
            f"%{p}x = OpCompositeExtract %float %{p}fc 0",
            f"%{p}y = OpCompositeExtract %float %{p}fc 1",
            f"%{p}xu = OpConvertFToU %uint %{p}x",
            f"%{p}yu = OpConvertFToU %uint %{p}y",
            f"%{p}xb = OpBitwiseAnd %uint %{p}xu %zq_u1",
            f"%{p}yb = OpBitwiseAnd %uint %{p}yu %zq_u1",
            f"%{p}y2 = OpShiftLeftLogical %uint %{p}yb %zq_u1"]


def convert(text, key):
    fc = re.search(r"OpDecorate (%\S+) BuiltIn FragCoord", text).group(1)
    sid = re.search(r"OpDecorate (%\S+) BuiltIn SubgroupLocalInvocationId", text).group(1)
    out, loads, shuffles, consts_done = [], 0, 0, False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("OpCapability GroupNonUniform"):
            continue
        if s.startswith("OpEntryPoint"):
            line = re.sub(r" " + re.escape(sid) + r"(?=\s|$)", "", line)
            s = line.strip()
        if s.startswith(("OpDecorate " + sid + " ", "OpName " + sid + " ")) or s.startswith(sid + " = OpVariable "):
            continue
        if re.match(r"%\S+ = OpFunction ", s) and not consts_done:
            out.append(CONSTS)
            consts_done = True
        m = re.match(r"(%\S+) = OpLoad %uint " + re.escape(sid) + "$", s)
        if m:
            p = "zl%d_" % loads
            out += lane(p, fc) + [f"{m.group(1)} = OpBitwiseOr %uint %{p}xb %{p}y2"]
            loads += 1
            continue
        m = re.match(r"(%\S+) = OpGroupNonUniformShuffle %float %\S+ (%\S+) (%\S+)$", s)
        if m:
            r, v, i = m.groups()
            p = "zs%d_" % shuffles
            out += lane(p, fc) + [
                f"%{p}ln = OpBitwiseOr %uint %{p}xb %{p}y2",
                f"%{p}d = OpBitwiseXor %uint {i} %{p}ln",
                f"%{p}dx = OpDPdx %float {v}",
                f"%{p}dy = OpDPdy %float {v}",
                f"%{p}xo = OpIEqual %bool %{p}xb %zq_u1",
                f"%{p}yo = OpIEqual %bool %{p}yb %zq_u1",
                f"%{p}sx = OpSelect %float %{p}xo %zq_fm1 %zq_f1",
                f"%{p}sy = OpSelect %float %{p}yo %zq_fm1 %zq_f1",
                f"%{p}hx = OpBitwiseAnd %uint %{p}d %zq_u1",
                f"%{p}hy = OpBitwiseAnd %uint %{p}d %zq_u2",
                f"%{p}bx = OpINotEqual %bool %{p}hx %zq_u0",
                f"%{p}by = OpINotEqual %bool %{p}hy %zq_u0",
                f"%{p}mx = OpFMul %float %{p}sx %{p}dx",
                f"%{p}my = OpFMul %float %{p}sy %{p}dy",
                f"%{p}ax = OpSelect %float %{p}bx %{p}mx %zq_f0",
                f"%{p}ay = OpSelect %float %{p}by %{p}my %zq_f0",
                f"%{p}s1 = OpFAdd %float {v} %{p}ax",
                f"{r} = OpFAdd %float %{p}s1 %{p}ay"]
            shuffles += 1
            continue
        if "OpGroupNonUniform" in s or sid in s:
            raise RuntimeError(f"{key}: unsupported form: {s}")
        out.append(line)
    if not loads or not shuffles:
        raise RuntimeError(f"{key}: loads={loads} shuffles={shuffles}")
    return "\n".join(out) + "\n"


count = 0
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for f in sorted(SHADERS.glob("*.xgs")):
        data = f.read_bytes()
        off = data.find(struct.pack("<I", 0x07230203))
        if off < 0 or not needs_override(array.array("I", data[off:])):
            continue
        spv, asm = tmp / "in.spv", tmp / "in.spvasm"
        spv.write_bytes(data[off:])
        tool("spirv-dis", spv, "-o", asm)
        asm.write_text(convert(asm.read_text(), f.stem))
        dst = OUT / (f.stem + ".spv")
        tool("spirv-as", "--target-env", "vulkan1.1", asm, "-o", dst)
        tool("spirv-val", "--target-env", "vulkan1.1", dst)
        count += 1
print("overrides written:", count)
