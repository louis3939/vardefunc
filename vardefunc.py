import vapoursynth as vs
import fvsfunc as fvf
import kagefunc as kgf
import havsfunc as hvf
from functools import partial
from vsutil import *

core = vs.core

# Broken. Don't use it.
def fade_filter(source: vs.VideoNode, clipa: vs.VideoNode, clipb: vs.VideoNode, 
                start: int = None, end: int = None, length: int = None)-> vs.VideoNode:

    if not length:
        length = end - start + 1

    length = length + 3

    black = core.std.BlankClip(source, format=vs.GRAY8, length=length, color=0)
    white = core.std.BlankClip(source, format=vs.GRAY8, length=length, color=255)
    
    fadmask = kgf.crossfade(black, white, length-1)
    
    fadmask = fadmask[2:-1]
    
    if get_depth(source) != 8:
        fadmask = fvf.Depth(fadmask, bits=get_depth(source))

    merged = source[:start]+core.std.MaskedMerge(clipa[start:end+1], clipb[start:end+1], fadmask)+source[end+1:]
    return merged


# Basically adaptive_grain of kagefunc with show_mask=True
def adaptive_mask(source: vs.VideoNode, luma_scaling: int = 12)-> vs.VideoNode:
    import numpy as np
    if get_depth(source) != 8:
        clip = fvf.Depth(source, bits=8)
    else:
        clip = source
    def fill_lut(y):
        x = np.arange(0, 1, 1 / (1 << 8))
        z = (1 - (x * (1.124 + x * (-9.466 + x * (36.624 + x * (-45.47 + x * 18.188)))))) ** ((y ** 2) * luma_scaling)
        if clip.format.sample_type == vs.INTEGER:
            z = z * 255
            z = np.rint(z).astype(int)
        return z.tolist()

    def generate_mask(n, f, clip):
        frameluma = round(f.props.PlaneStatsAverage * 999)
        table = lut[int(frameluma)]
        return core.std.Lut(clip, lut=table)

    lut = [None] * 1000
    for y in np.arange(0, 1, 0.001):
        lut[int(round(y * 1000))] = fill_lut(y)

    luma = get_y(fvf.Depth(clip, 8)).std.PlaneStats()

    mask = core.std.FrameEval(luma, partial(generate_mask, clip=luma), prop_src=luma)
    mask = core.resize.Spline36(mask, clip.width, clip.height)

    if get_depth(source) != 8:
        mask = fvf.Depth(mask, bits=get_depth(source))
    return mask

def KNLMCL(source: vs.VideoNode, h_Y: float = 1.2, h_UV: float = 0.5, device_id: int = 0, depth: int = None)-> vs.VideoNode:
    
    if get_depth(source) != 32:
        clip = fvf.Depth(source, 32)
    else:
        clip = source
    
    denoise = core.knlm.KNLMeansCL(clip, a=2, h=h_Y, d=3, device_type='gpu', device_id=device_id, channels='Y')
    denoise = core.knlm.KNLMeansCL(denoise, a=2, h=h_UV, d=3, device_type='gpu', device_id=device_id, channels='UV')

    if depth is not None:
        denoise = fvf.Depth(denoise, depth)
    
    return denoise

# Modified version of atomchtools
def DiffRescaleMask(source: vs.VideoNode, h: int = 720, kernel: str = 'bicubic', 
                    b:float = 1/3, c:float = 1/3, mthr: int = 55, 
                    mode: str = 'rectangle', sw: int = 2, sh: int = 2)-> vs.VideoNode:

    only_luma = source.format.num_planes == 1

    if get_depth(source) != 8:
        clip = fvf.Depth(source, 8)
    else:
        clip = source

    if not only_luma:
        clip = get_y(clip)

    w = get_w(h)
    desc = fvf.Resize(clip, w, h, kernel=kernel, a1=b, a2=c, invks=True)
    upsc = fvf.Depth(fvf.Resize(desc, source.width, source.height, kernel=kernel, a1=b, a2=c), 8)
    
    diff = core.std.MakeDiff(clip, upsc)
    mask = diff.rgvs.RemoveGrain(2).rgvs.RemoveGrain(2).hist.Luma()
    mask = mask.std.Expr('x {thr} < 0 x ?'.format(thr=mthr))
    mask = mask.std.Prewitt().std.Maximum().std.Maximum().std.Deflate()
    mask = hvf.mt_expand_multi(mask, mode=mode, sw=sw, sh=sh)

    if get_depth(source) != 8:
        mask = fvf.Depth(mask, bits=get_depth(source))
    return mask

DRM = DiffRescaleMask

# Modified version of atomchtools
def DiffCreditlessMask(source: vs.VideoNode, titles: vs.VideoNode, nc: vs.VideoNode, 
                        start: int = None, end: int = None, 
                        sw: int = 2, sh: int = 2)-> vs.VideoNode:

    if get_depth(titles) != 8:
        titles = fvf.Depth(titles, 8)
    if get_depth(nc) != 8:
        nc = fvf.Depth(nc, 8)

    diff = core.std.MakeDiff(titles, nc, [0])
    diff = get_y(diff)
    diff = diff.std.Prewitt().std.Expr('x 25 < 0 x ?').std.Expr('x 2 *')
    diff = core.rgvs.RemoveGrain(diff, 4).std.Expr('x 30 > 255 x ?')

    credit_m = hvf.mt_expand_multi(diff, sw=sw, sh=sh)

    blank = core.std.BlankClip(source, format=vs.GRAY8)

    if start == 0:
        credit_m = credit_m+blank[end+1:]
    elif end == source.num_frames-1:
        credit_m = blank[:start]+credit_m
    else:
        credit_m = blank[:start]+credit_m+blank[end+1:]

    if get_depth(source) != 8:
        credit_m = fvf.Depth(credit_m, bits=get_depth(source))
    return credit_m

DCM = DiffCreditlessMask

def F3kdbSep(src_y: vs.VideoNode, src_uv: vs.VideoNode, 
            range: int = None, y: int = None, c: int = None,
            grainy: int = None, grainc: int = None,
            mask: vs.VideoNode = None, neo_f3kdb: bool = True)-> List[vs.VideoNode]:

    only_luma = src_y.format.num_planes == 1

    if not only_luma:
        src_y = get_y(src_y)

    if get_depth(src_y) != 16:
        src_y = fvf.Depth(src_y, 16)
    if get_depth(src_uv) != 16:
        src_uv = fvf.Depth(src_uv, 16)

    if neo_f3kdb:
        db_y = core.neo_f3kdb.Deband(src_y, range, y, grainy=grainy, sample_mode=4, preset='luma')
        db_c = core.neo_f3kdb.Deband(src_uv, range, cb=c, cr=c, grainc=grainc, sample_mode=4, preset='chroma')
    else:
        db_y = core.f3kdb.Deband(src_y, range, y, grainy=grainy, output_depth=16, preset='luma')
        db_c = core.f3kdb.Deband(src_uv, range, cb=c, cr=c, grainc=grainc, output_depth=16, preset='chroma')

    if mask is not None:
        if get_depth(mask) != 16:
            mask = fvf.Depth(mask, 16)
        if mask.height != src_y.height:
            mask_y = core.resize.Bicubic(mask, src_y.width, src_y.height)
        else:
            mask_y = mask
        db_y = core.std.MaskedMerge(db_y, src_y, mask_y, 0)

        if mask.height != src_uv.height:
            mask_c = core.resize.Bicubic(mask, src_uv.width, src_uv.height)
        else:
            mask_c = mask
        db_c = core.std.MaskedMerge(db_c, src_uv, mask_c, [1, 2])

    return db_y, db_c

#Zastin’s nnedi3 chroma upscaler
def to444(clip, w=None, h=None, join=True):
    
    uv = [nnedi3x2(c) for c in kgf.split(clip)[1:]]
    
    if w in (None, clip.width) and h in (None, clip.height):
        uv = [core.fmtc.resample(c, sy=0.5, flt=0) for c in uv]
    else:
        uv = [core.resize.Spline36(c, w, h, src_top=0.5) for c in uv]
    
    return core.std.ShufflePlanes([clip] + uv, [0]*3, vs.YUV) if join else uv

def nnedi3x2(clip):
    if hasattr(core, 'znedi3'):
        return clip.std.Transpose().znedi3.nnedi3(1, 1, 0, 0, 4, 2).std.Transpose().znedi3.nnedi3(0, 1, 0, 0, 4, 2)
    else:
        return clip.std.Transpose().nnedi3.nnedi3(1, 1, 0, 0, 3, 1).std.Transpose().nnedi3.nnedi3(0, 1, 0, 0, 3, 1)

# Modified version of kagefunc without the header text
def generate_keyframes(clip: vs.VideoNode, out_path=None) -> None:
    import os
    clip = core.resize.Bilinear(clip, 640, 360)
    clip = core.wwxd.WWXD(clip)
    out_txt = ""
    for i in range(clip.num_frames):
        if clip.get_frame(i).props.Scenechange == 1:
            out_txt += "%d I -1\n" % i
        if i % 1000 == 0:
            print(i)
    out_path = fallback(out_path, os.path.expanduser("~") + "/Desktop/keyframes.log")
    text_file = open(out_path, "w")
    text_file.write(out_txt)
    text_file.close()
    
def RegionMask(clip: vs.VideoNode, left: int = None, right: int = None, top: int = None, bottom: int = None)-> vs.VideoNode:
    crop = core.std.Crop(clip, left, right, top, bottom)
    borders = core.std.AddBorders(crop, left, right, top, bottom)
    return borders

def GetChromaShift(src_h: int = None, dst_h: int = None, aspect_ratio: float = 16/9) -> float:
    src_w = get_w(src_h, aspect_ratio)
    dst_w = get_w(dst_h, aspect_ratio)
    
    ch_shift = 0.25 - 0.25 * (src_w / dst_w)
    ch_shift = float(round(ch_shift, 5))
    return ch_shift
