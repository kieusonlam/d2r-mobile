#!/usr/bin/env python3
# 选项页签底图加宽：页签宽 266 → 340（touch_layout.SETTINGS_TAB_W）后，原版底图 StashTabs 是定宽贴图、不随 tabSize 伸缩，字跑出底图。
# 这里从原图逐帧出一张同款的宽图：左右边框各 4 列与正中的宝石原样保留，两侧石纹横向拉伸（纹理是噪点，拉 28% 看不出）。
# 储藏箱页签也用 StashTabs，所以不覆盖原图，新图放新增路径，只有选项页签条改指过去。编成原包同款 DXT5 + 块线性（gen_widestash_art.bc3 / pack）。
# Widened options tab background: with tabs widened 266 → 340 (touch_layout.SETTINGS_TAB_W), the original StashTabs background is a fixed-width sprite that does not
# stretch with tabSize, so the text runs off it. This builds a wide image of the same kind frame by frame from the original: the 4 border columns on each side and
# the centre gem are kept as is, and the stone texture on both sides is stretched horizontally (the texture is noise; 28% is not noticeable).
# The stash tabs also use StashTabs, so the original is not overwritten: the new image goes to a new path and only the options tab bar points there.
# Encoded as DXT5 + block-linear like the original package (gen_widestash_art.bc3 / pack).
import os
from PIL import Image
import romfs_art, touch_layout, gen_widestash_art as G

SRC = 'hd/global/ui/Controller/Panel/Stash/V2/StashTabs.sprite'
DST = 'controller/panel/zswide/dmoptiontabs'   # 新增路径（与 wide_stash 同一个新子目录） | Newly added path (same new subdirectory as wide_stash)
FRAMES, BORDER, GEM = 4, 4, 10                 # 帧数；两侧保留的边框列数；正中保留的宝石半宽 | frame count; border columns kept on each side; half-width of the centre gem kept

def _widen(fr, nw):
    w, h = fr.size; cx = w // 2; lw = (nw - 2 * BORDER - 2 * GEM) // 2; rw = nw - 2 * BORDER - 2 * GEM - lw
    out = Image.new('RGBA', (nw, h))
    out.paste(fr.crop((0, 0, BORDER, h)), (0, 0))
    out.paste(fr.crop((BORDER, 0, cx - GEM, h)).resize((lw, h), Image.LANCZOS), (BORDER, 0))
    out.paste(fr.crop((cx - GEM, 0, cx + GEM, h)), (BORDER + lw, 0))
    out.paste(fr.crop((cx + GEM, 0, w - BORDER, h)).resize((rw, h), Image.LANCZOS), (BORDER + lw + 2 * GEM, 0))
    out.paste(fr.crop((w - BORDER, 0, w, h)), (nw - BORDER, 0))
    return out

def build(romfs, outdir):
    b = romfs_art.read(romfs, SRC); im = romfs_art.sprite(romfs, SRC); fw = int.from_bytes(b[6:8], 'little')
    assert (im.width, im.height, fw) == (539, 60, 134), (im.size, fw)   # 原版尺寸变了就停在这里 | stop here if the original size changed
    nw = round(fw * touch_layout.SETTINGS_TAB_W / 266)
    sheet = Image.new('RGBA', (nw * FRAMES, im.height))
    for i in range(FRAMES):
        x0 = round(i * im.width / FRAMES)   # 原图帧距 539 / 4，不是整数 | the original frame pitch is 539 / 4, not an integer
        sheet.paste(_widen(im.crop((x0, 0, x0 + fw, im.height)), nw), (i * nw, 0))
    p = os.path.join(outdir, 'dm_optiontabs.sprite')
    open(p, 'wb').write(G.pack(G.lib(), b, G.bc3(sheet), sheet.width, sheet.height, nw, FRAMES))
    return {'+data/hd/global/ui/' + DST + '.sprite': p}
