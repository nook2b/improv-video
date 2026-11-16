"""Значки меню из иконок Lucide (как в дизайн-системе): python tools/make_icons.py resources/icons

Нужен Chromium headless_shell (в облачной среде — /opt/pw-browsers). Результат — чёрные PNG-шаблоны.
"""
import subprocess, sys, pathlib
HS = next(pathlib.Path("/opt/pw-browsers").glob("chromium_headless_shell-*/*/headless_shell"))
out = pathlib.Path(sys.argv[1]).resolve()
P = {
 "film": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18"/><path d="M3 7.5h4"/><path d="M3 12h18"/><path d="M3 16.5h4"/><path d="M17 3v18"/><path d="M17 7.5h4"/><path d="M17 16.5h4"/>',
 "circle-alert": '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
 "circle-check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
 "check": '<path d="M20 6 9 17l-5-5"/>',
 "loader": '<path d="M12 2v4"/><path d="m16.2 7.8 2.9-2.9"/><path d="M18 12h4"/><path d="m16.2 16.2 2.9 2.9"/><path d="M12 18v4"/><path d="m4.9 19.1 2.9-2.9"/><path d="M2 12h4"/><path d="m4.9 4.9 2.9 2.9"/>',
 "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
 "external-link": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
 "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
 "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
 "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
 "folder-open": '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
 "youtube": '<path d="M2.5 17a24.12 24.12 0 0 1 0-10 2 2 0 0 1 1.4-1.4 49.56 49.56 0 0 1 16.2 0A2 2 0 0 1 21.5 7a24.12 24.12 0 0 1 0 10 2 2 0 0 1-1.4 1.4 49.55 49.55 0 0 1-16.2 0A2 2 0 0 1 2.5 17"/><path d="m10 15 5-3-5-3z"/>',
}
def svg(body, stroke=1.6, extra=""):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="100%" height="100%" fill="none" '
            f'stroke="black" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{extra}{body}</svg>')
def render(name, svgtext, px):
    html = out / f"{name}.html"
    html.write_text(f'<html><body style="margin:0;background:transparent"><div style="width:{px}px;height:{px}px">{svgtext}</div></body></html>')
    subprocess.run([str(HS), "--no-sandbox", "--hide-scrollbars", "--default-background-color=00000000",
                    f"--window-size={px},{px}", f"--screenshot={out/(name+'.png')}", html.as_uri()],
                   check=True, capture_output=True)
    html.unlink()
# Значок строки меню: 18 pt, @2x = 36 px
film = P["film"]
render("menubar", svg(film), 36)
# «ждёт человека»: точка вверху справа с вырезом 1.5
mask_dot = '<defs><mask id="m"><rect width="24" height="24" fill="white"/><circle cx="21" cy="3.6" r="5.3" fill="black"/></mask></defs>'
render("menubar-dot", svg(f'<g mask="url(#m)">{film}</g><circle cx="21" cy="3.6" r="3.2" fill="black" stroke="none"/>', extra=mask_dot), 36)
# «ошибка»: circle-alert 11 pt внизу справа с вырезом
mask_alert = '<defs><mask id="m"><rect width="24" height="24" fill="white"/><circle cx="18.6" cy="18.6" r="6.9" fill="black"/></mask></defs>'
badge = '<g transform="translate(13.2 13.2) scale(0.45)" stroke-width="3.8">' + P["circle-alert"] + '</g>'
render("menubar-alert", svg(f'<g mask="url(#m)">{film}</g>{badge}', extra=mask_alert), 36)
for name, pt in [("check", 12), ("loader", 12), ("clock", 12), ("external-link", 12), ("arrow-right", 11),
                 ("circle-check", 13), ("circle-alert", 13), ("youtube", 16), ("film", 18),
                 ("x", 14), ("copy", 14), ("folder-open", 14)]:
    render(name, svg(P[name], stroke=2.2 if name == "check" else 1.6), pt * 3)
