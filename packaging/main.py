"""Точка входа собранного приложения."""

import sys

if __name__ == "__main__":
    if "--bench" in sys.argv:
        from improv_video.bench import main

        sys.exit(main(sys.argv[sys.argv.index("--bench") + 1:]))
    if "--selftest" in sys.argv:
        from improv_video.selftest import main

        sys.exit(main())
    from improv_video.app.menubar import main

    main()
