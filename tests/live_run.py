import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from _harness import install
install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main

async def run():
    plugin = main.Plugin()
    await plugin._main()
    state = await plugin.get_state()
    print(json.dumps(state, indent=2, sort_keys=True))
    print("hidraw:", main._vendor_hidraw())
    print("controller packet:", main.controller_command(state["controller"]).hex())

asyncio.run(run())
