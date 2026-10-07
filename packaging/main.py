"""Точка входа собранного приложения."""

import sys
from pathlib import Path

if __name__ == "__main__":
    if "--bench" in sys.argv:
        from improv_video.bench import main

        sys.exit(main(sys.argv[sys.argv.index("--bench") + 1:]))
    if "--render-ui" in sys.argv:
        from AppKit import NSApplication

        from improv_video.app.menubar import render_selftest

        NSApplication.sharedApplication()
        sys.exit(render_selftest(Path(sys.argv[sys.argv.index("--render-ui") + 1])))
    if "--selftest" in sys.argv:
        from improv_video.selftest import main

        sys.exit(main())
    from improv_video.app.menubar import main

    main()
