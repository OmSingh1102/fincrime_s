"""SVG document images to PNG, for the AI Agent to send to Claude (the model
reads PNG, not SVG). resvg draws the blur, glare and crop effects the
documents carry, so a blurred ID reaches the model blurred."""


def svg_to_png(svg, zoom=1.5):
    import resvg_py
    return bytes(resvg_py.svg_to_bytes(svg_string=svg, zoom=zoom))
