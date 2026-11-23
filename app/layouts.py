"""ClipForge layout geometry, shared by browser preview and FFmpeg.

Ported from NeonClipping's layouts.py (CATALOG, geometry, render_graph,
output_settings, text_positions) and extended with PeakCut's zoom / fit /
split / left / right modes. Every render_graph ends in a [base] stream.
"""
import math

CATALOG = [
    ('auto', 'Auto (smart)', 'Picks the look per clip: facecam stack, face follow, or blurred fill'),
    ('stack', 'Face + gameplay', 'Camera above the action'),
    ('bottom', 'Gameplay + face', 'Camera below the action'),
    ('follow', 'Follow face', 'A 9:16 window that glides after the face'),
    ('blur', 'Blurred fill', 'Keep the complete source'),
    ('zoom', 'Zoomed + blurred fill', 'Zoomed foreground on blurred fill'),
    ('fill', 'Fill screen', 'Full-frame centre crop'),
    ('left', 'Fill, keep left', 'Full-frame crop anchored left'),
    ('right', 'Fill, keep right', 'Full-frame crop anchored right'),
    ('fit', 'Full frame on black', 'Full frame fitted on black'),
    ('game', 'Gameplay only', 'Use your selected game area'),
    ('pip_tl', 'Camera corner · top-left', 'Floating camera top-left'),
    ('pip_tr', 'Camera corner · top-right', 'Floating camera top-right'),
    ('pip_bl', 'Camera corner · bottom-left', 'Floating camera bottom-left'),
    ('pip_br', 'Camera corner · bottom-right', 'Floating camera bottom-right'),
    ('circle', 'Circle camera', 'Round facecam above gameplay'),
    ('side', 'Side by side', 'Camera and gameplay columns'),
    ('split', 'Split screen (two people)', 'Left half on top, right half below'),
]

ASPECTS = {'vertical': (1080, 1920), 'square': (1080, 1080), 'portrait': (1080, 1350),
           'landscape': (1920, 1080), 'threefour': (1080, 1440)}


def output_settings(body):
    """(width, height, fps) from an options dict, validated like NeonClipping."""
    body = body or {}
    aspect = str(body.get('aspect', 'vertical'))
    if aspect not in ASPECTS:
        raise ValueError('Choose a supported aspect ratio.')
    resolution = int(body.get('resolution', 1080))
    fps = int(body.get('fps') or 0)
    if resolution not in (720, 1080, 2160) or fps not in (0, 24, 30, 60):
        raise ValueError('Choose 720, 1080 or 2160 and a supported frame rate.')
    width, height = ASPECTS[aspect]
    scale = resolution / 1080
    return int(width * scale) // 2 * 2, int(height * scale) // 2 * 2, fps


def _even(v):
    return max(2, int(round(v)) // 2 * 2)


def geometry(name, top=0.32, width=1080, height=1920):
    """Where each layer sits in the output frame.

    Returns {"mode", "layers", "width", "height"} (+ "fg_h" for blur/zoom/fit).
    Modes: "layers" (NeonClipping layered crops), "blur", "zoom", "fit",
    "split". left/right are a single full layer with an "anchor" key.
    """
    if name not in {x[0] for x in CATALOG}:
        raise ValueError('Choose a valid layout.')
    top = float(top)
    if not math.isfinite(top) or not .2 <= top <= .5:
        raise ValueError('Face size must be between 20% and 50%.')
    h = int(top * height) // 2 * 2
    full = {'source': 'full', 'x': 0, 'y': 0, 'w': width, 'h': height}
    game = {**full, 'source': 'game'}
    if name == 'auto':
        # Resolved per clip at render time (facecam.auto_look); geometry here
        # is the fallback (blur) so previews never crash.
        return {'mode': 'auto', 'layers': [], 'width': width, 'height': height,
                'fg_h': min(height, _even(width * 9 / 16))}
    if name == 'follow':
        return {'mode': 'follow', 'layers': [], 'width': width, 'height': height}
    if name == 'blur':
        # Foreground height assumes a 16:9 source (geometry has no source size);
        # render_graph fits the real frame with scale=decrease.
        return {'mode': 'blur', 'layers': [], 'width': width, 'height': height,
                'fg_h': min(height, _even(width * 9 / 16))}
    if name == 'zoom':
        return {'mode': 'zoom', 'layers': [], 'width': width, 'height': height,
                'fg_h': _even(1000 * height / 1920)}
    if name == 'fit':
        return {'mode': 'fit', 'layers': [], 'width': width, 'height': height,
                'fg_h': min(height, _even(width * 9 / 16))}
    if name == 'split':
        return {'mode': 'split', 'layers': [], 'width': width, 'height': height}
    if name in ('fill', 'game'):
        layers = [full if name == 'fill' else game]
    elif name in ('left', 'right'):
        layers = [{**full, 'anchor': name}]
    elif name in ('stack', 'bottom'):
        cam = {'source': 'cam', 'x': 0, 'y': 0 if name == 'stack' else height - h, 'w': width, 'h': h}
        layers = [{**game, 'y': h if name == 'stack' else 0, 'h': height - h}, cam]
    elif name == 'side':
        w = int(top * width) // 2 * 2
        layers = [{**game, 'x': w, 'w': width - w}, {'source': 'cam', 'x': 0, 'y': 0, 'w': w, 'h': height}]
    else:
        # A wide output must not make a square facecam taller than the frame.
        w = min(int(top * width) // 2 * 2, int(height * .45) // 2 * 2)
        size = w if name == 'circle' else int(w * .9) // 2 * 2
        margin = int(width * 36 / 1080) // 2 * 2
        layers = [game, {'source': 'cam',
                         'x': width - w - margin if name in ('pip_tr', 'pip_br') else margin,
                         'y': height - size - round(180 * height / 1920) if name in ('pip_bl', 'pip_br')
                         else round(72 * height / 1920),
                         'w': w, 'h': size, 'circle': name == 'circle'}]
    return {'mode': 'layers', 'layers': layers, 'width': width, 'height': height}


def text_positions(name, top=0.32, caption_y=1450, width=1080, height=1920):
    """{"hook_y", "caption_y"} in output pixels.

    NeonClipping's logic, extended: blur/zoom/fit place the hook above the
    foreground and captions below it (PeakCut geometry); split puts captions
    on the seam between the two halves.
    """
    spec = geometry(name, top, width, height)
    sy = height / 1920.0
    if name in ('auto', 'follow'):
        return {'hook_y': round(230 * sy), 'caption_y': round(float(caption_y) * sy)}
    if name in ('blur', 'zoom', 'fit'):
        fg_h = spec.get('fg_h', height)
        fg_top = (height - fg_h) // 2
        hook_y = max(round(230 * sy), fg_top - round(190 * sy))
        if int(caption_y) == 1450:
            position = min(round(1640 * sy), fg_top + fg_h + round(180 * sy))
        else:
            position = round(float(caption_y) * sy)
        return {'hook_y': round(hook_y), 'caption_y': round(position)}
    if name == 'split':
        return {'hook_y': round(230 * sy), 'caption_y': height // 2}
    cam = next((layer for layer in spec['layers'] if layer['source'] == 'cam'), None)
    hook_y = 260 * sy
    position = float(caption_y) * sy
    if cam and cam['y'] < 100 * sy:
        hook_y = cam['y'] + cam['h'] + 30 * sy
    if name == 'side':
        hook_y = 260 * sy
    if int(caption_y) == 1450 and cam and cam['y'] > 900 * sy:
        position = min(position, cam['y'] - 120 * sy)
    return {'hook_y': round(hook_y), 'caption_y': round(position)}


def _blur_bg(width, height):
    bw, bh = max(2, width // 8 * 2), max(2, height // 8 * 2)
    return (f'[b]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},'
            f'boxblur=8:2,scale={width}:{height}[bg]')


def _follow_graph(follow, width, height, src_h):
    """9:16 window gliding after the face: crop with a cosine-eased x
    expression built from (t, x) keyframes. Ported from stream-clips
    frame.crop_graph(). follow = (keys, crop_w) in source pixels."""
    keys, cw = follow
    if not keys:
        keys = [(0.0, 0.0)]
    expr = f"{keys[-1][1]:.1f}"
    for (t0, a), (t1, b) in reversed(list(zip(keys, keys[1:]))):
        span = max(t1 - t0, 0.05)
        expr = (f"if(lt(t,{t1:.2f}),{a:.1f}+({b - a:.1f})*"
                f"(1-cos(PI*min(1,(t-{t0:.2f})/{span:.2f})))/2,{expr})")
    return (f"[0:v]crop={int(cw)}:{int(src_h)}:x='{expr}':y=0,"
            f"scale={width}:{height}:flags=lanczos,setsar=1[base]")


def _game_crop_x(w, h, src_w, src_h, cam_rect):
    """Crop x (in scaled-frame pixels) for a full-frame game layer.

    The game layer is scaled to cover (w, h), which usually leaves the
    scaled frame wider than the panel. Centre the window on the action
    (games put the player/crosshair mid-frame), slid just enough to keep
    the facecam rect out of the shot so the webcam is not shown twice.
    Hard-anchoring to an edge would throw the centre of the game away."""
    scale = max(w / src_w, h / src_h)
    sw = src_w * scale
    x = (sw - w) / 2.0
    cx, _cy, cw_px, _ch = cam_rect
    cam_l, cam_r = cx * scale, (cx + cw_px) * scale
    if (cam_l + cam_r) / 2.0 > sw / 2.0:      # cam on the right: slide left
        x = min(x, cam_l - w)
    else:                                       # cam on the left: slide right
        x = max(x, cam_r)
    return max(0.0, min(x, sw - w))


def render_graph(name, rects_px, top=0.32, width=1080, height=1920, cam_fx='',
                 follow=None, src_h=0, src_w=0):
    """FFmpeg filtergraph from [0:v] to [base] for one layout.

    rects_px is (cam_rect, game_rect), each (x, y, w, h) in SOURCE pixels or
    None. A layer whose rect is None uses the full frame (no crop).
    cam_fx is an extra filter string appended after the cam layer's scale
    (e.g. "eq=contrast=1.05,unsharp=5:5:0.5"; a leading comma is optional).
    follow: (keys, crop_w) for the follow layout (facecam.follow_keys).
    src_w/src_h: source size, used to place the gameplay window (centre
    on the action, slid just enough to keep the facecam out of shot).
    """
    spec = geometry(name, top, width, height)
    rects_px = rects_px or (None, None)
    if spec['mode'] == 'follow':
        if follow and src_h:
            return _follow_graph(follow, width, height, src_h)
        # No track: centre crop (same as fill).
        return (f'[0:v]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,'
                f'crop={width}:{height},setsar=1[base]')
    if spec['mode'] == 'auto':
        # Unresolved auto (render.py resolves it per clip); fall back to blur.
        return (f'[0:v]split=2[b][f];{_blur_bg(width, height)};'
                f'[f]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];'
                f'[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]')
    if spec['mode'] == 'blur':
        return (f'[0:v]split=2[b][f];{_blur_bg(width, height)};'
                f'[f]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];'
                f'[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]')
    if spec['mode'] == 'zoom':
        fg_h = spec['fg_h']
        return (f'[0:v]split=2[b][f];{_blur_bg(width, height)};'
                f'[f]scale={width}:{fg_h}:force_original_aspect_ratio=increase,crop={width}:{fg_h}[fg];'
                f'[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]')
    if spec['mode'] == 'fit':
        return (f'[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,'
                f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[base]')
    if spec['mode'] == 'split':
        top_h = height // 2
        bot_h = height - top_h
        return (f'[0:v]split=2[l][r];'
                f'[l]crop=iw/2:ih:0:0,scale={width}:{top_h}:force_original_aspect_ratio=increase,'
                f'crop={width}:{top_h}[t];'
                f'[r]crop=iw/2:ih:iw/2:0,scale={width}:{bot_h}:force_original_aspect_ratio=increase,'
                f'crop={width}:{bot_h}[u];'
                f'[t][u]vstack,setsar=1[base]')
    layers = spec['layers']
    parts = []
    if len(layers) > 1:
        parts.append('[0:v]split=' + str(len(layers)) + ''.join(f'[s{i}]' for i in range(len(layers))))
    fx = ''
    if cam_fx:
        fx = str(cam_fx).strip()
        if fx and not fx.startswith(','):
            fx = ',' + fx
    for i, layer in enumerate(layers):
        source = f'[s{i}]' if len(layers) > 1 else '[0:v]'
        crop = ''
        if layer['source'] != 'full':
            rect = rects_px[0] if layer['source'] == 'cam' else rects_px[1]
            if rect is not None:
                x, y, w, h = (int(v) for v in rect)
                crop = f'crop={w}:{h}:{x}:{y},'
        w, h = layer['w'], layer['h']
        anchor = layer.get('anchor')
        # Gameplay window: centre on the action, slid just enough to keep
        # the facecam out of the shot (so the webcam isn't shown twice).
        # Hard-anchoring to an edge throws the centre of the game away.
        game_x = None
        if (anchor is None and layer['source'] == 'game' and rects_px[1] is None
                and rects_px[0] is not None and src_w and src_h):
            game_x = _game_crop_x(w, h, src_w, src_h, rects_px[0])
        if game_x is not None:
            fit_crop = f"crop={w}:{h}:x='min(max({game_x:.0f},0),in_w-out_w)':y=0"
        elif anchor == 'left':
            fit_crop = f'crop={w}:{h}:0:0'
        elif anchor == 'right':
            fit_crop = f'crop={w}:{h}:in_w-out_w:0'
        else:
            fit_crop = f'crop={w}:{h}'
        mask = ''
        if layer.get('circle'):
            mask = (",format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                    "a='if(lte(pow(X-W/2,2)+pow(Y-H/2,2),pow(min(W,H)/2,2)),255,0)'")
        look = fx if layer['source'] == 'cam' else ''
        parts.append(source + crop +
                     f'scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,{fit_crop},setsar=1'
                     + look + mask + f'[p{i}]')
    first = layers[0]
    parts.append(f"[p0]pad={width}:{height}:{first['x']}:{first['y']}:black[b0]")
    for i, layer in enumerate(layers[1:], 1):
        parts.append(f"[b{i - 1}][p{i}]overlay={layer['x']}:{layer['y']}:shortest=1[b{i}]")
    parts.append(f'[b{len(layers) - 1}]null[base]')
    return ';'.join(parts)


def needs_cam(name):
    """True when the layout draws a separate camera layer."""
    try:
        return any(layer['source'] == 'cam' for layer in geometry(name)['layers'])
    except ValueError:
        return False


def needs_game(name):
    """True when the layout draws a separate gameplay layer."""
    try:
        return any(layer['source'] == 'game' for layer in geometry(name)['layers'])
    except ValueError:
        return False
