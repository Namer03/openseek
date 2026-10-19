import sys

if "--demo" in sys.argv:        # offline demo on synthetic sample data (see openseek_desktop/demo.py)
    from openseek_desktop import demo
    demo.install()
    if "--shots" in sys.argv:
        demo.run_shots(sys.argv[sys.argv.index("--shots") + 1])
        sys.exit(0)
from openseek_desktop.main import main
sys.exit(main())
