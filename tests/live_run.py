import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import support  # noqa: F401  (the Decky harness before any backend import)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main
import controller_backend

async def run():
    plugin = main.Plugin()
    await plugin._main()
    state = await plugin.get_state()
    print(json.dumps(state, indent=2, sort_keys=True))
    print("hidraw:", controller_backend.vendor_hidraw())
    print("controller packet:", controller_backend.controller_command(state["controller"]).hex())

asyncio.run(run())
