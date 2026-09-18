"""
Generated Script: robot_ping
Description: 檢查 V4.9 機器人核心與語義層狀態
"""
import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

try:
    from skills.nav_core import NavBrain
except ImportError:
    NavBrain = None

def execute():
    brain = NavBrain()
    return brain.get_v49_status()

if __name__ == "__main__":
    try:
        args = sys.argv[1:]
        print(execute(*args))
    except Exception as e:
        print(f"Error executing robot_ping: {e}", file=sys.stderr)
        sys.exit(1)
