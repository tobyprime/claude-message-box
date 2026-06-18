"""DingTalk source — connects to DingTalk Stream Mode and forwards to msgbox DB."""

import logging
import subprocess
import sys
import threading
from pathlib import Path

from .. import config

logger = logging.getLogger("msgbox.sources.dingtalk")

DINGTALK_JS = Path(__file__).parent / "dingtalk.js"


def cmd_source_dingtalk(args):
    """Start the DingTalk stream client."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    client_id = args.client_id or ""
    client_secret = args.client_secret or ""

    if not client_id:
        client_id = input("DingTalk Client ID: ").strip()
    if not client_secret:
        import getpass
        client_secret = getpass.getpass("DingTalk Client Secret: ").strip()

    if not client_id or not client_secret:
        print("Client ID and Secret are required", file=sys.stderr)
        sys.exit(1)

    logger.info("DingTalk source starting...")

    def run_node():
        cmd = [
            "node", str(DINGTALK_JS),
            str(config.CENTRAL_DB),
            client_id, client_secret,
        ]
        proc = subprocess.Popen(cmd, stdout=sys.stderr, stderr=sys.stderr)
        proc.wait()

    t = threading.Thread(target=run_node, daemon=True)
    t.start()

    if args.foreground:
        try:
            t.join()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
    else:
        logger.info("DingTalk source started in background")

    if args.save_config:
        from ..yaml_config import set_config_value
        set_config_value("sources.dingtalk.client_id", client_id)
        logger.info("Saved DingTalk Client ID to config")
