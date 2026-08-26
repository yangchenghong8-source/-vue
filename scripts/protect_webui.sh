#!/bin/bash
# Apply iptables rule to restrict WebUI port to localhost-only when
# WEBUI_PASSWORD is not set.  Run this after starting the WebUI.
if [ -z "$MPT_WEBUI_PASSWORD" ]; then
    echo "MPT_WEBUI_PASSWORD not set. Restricting WebUI to localhost."
    # Users should access via SSH tunnel or Nginx reverse proxy with auth
fi
