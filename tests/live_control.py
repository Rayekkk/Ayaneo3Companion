import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import support  # noqa: F401  (the Decky harness before any backend import)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main
import controller_backend

backup = "/tmp/aya3-controller-before.json"
action = sys.argv[1]
if action == "test":
    if os.path.exists(backup):
        raise SystemExit(f"{backup} exists: run restore first, a second test would overwrite the original")
    current = controller_backend.read_controller()
    with open(backup, "w", encoding="utf-8") as output:
        json.dump(current, output)
    test = dict(current, vibration="low", rgb_mode="solid", color="ff0000", brightness=100)
    controller_backend.apply_controller(test)
    print("before:", current)
    print("test:", controller_backend.read_controller())
elif action == "restore":
    # Restore what "test" saved, not a guess: the owner's colour and vibration differ per console.
    with open(backup, encoding="utf-8") as source:
        previous = json.load(source)
    controller_backend.apply_controller(previous)
    os.remove(backup)
    print("restored:", controller_backend.read_controller())
else:
    raise SystemExit("test or restore required")
