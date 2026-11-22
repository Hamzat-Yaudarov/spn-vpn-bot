#!/bin/sh
set -eu

credentials=${CREDENTIALS_DIRECTORY:?systemd credentials directory is required}

REMNAWAVE_API_TOKEN=$(cat "$credentials/rotation_remnawave")
export REMNAWAVE_API_TOKEN

if [ -f "$credentials/rotation_cloudflare" ]; then
    CLOUDFLARE_API_TOKEN=$(cat "$credentials/rotation_cloudflare")
    export CLOUDFLARE_API_TOKEN
fi

if [ -f "$credentials/rotation_reg-username" ] && [ -f "$credentials/rotation_reg-password" ]; then
    REG_RU_USERNAME=$(cat "$credentials/rotation_reg-username")
    REG_RU_PASSWORD=$(cat "$credentials/rotation_reg-password")
    export REG_RU_USERNAME REG_RU_PASSWORD
fi

if [ -f "$credentials/rotation_xorek-session" ]; then
    XOREK_BILLMANAGER_SESSION=$(cat "$credentials/rotation_xorek-session")
    export XOREK_BILLMANAGER_SESSION
fi

if [ -f "$credentials/rotation_xorek-username" ] && [ -f "$credentials/rotation_xorek-password" ]; then
    XOREK_BILLMANAGER_USERNAME=$(cat "$credentials/rotation_xorek-username")
    XOREK_BILLMANAGER_PASSWORD=$(cat "$credentials/rotation_xorek-password")
    export XOREK_BILLMANAGER_USERNAME XOREK_BILLMANAGER_PASSWORD
fi

if [ -f "$credentials/rotation_onecent-email" ] && [ -f "$credentials/rotation_onecent-password" ]; then
    ONECENT_EMAIL=$(cat "$credentials/rotation_onecent-email")
    ONECENT_PASSWORD=$(cat "$credentials/rotation_onecent-password")
    export ONECENT_EMAIL ONECENT_PASSWORD
fi

if [ -f /etc/spn-ip-failover/xorek-order.json ]; then
    XOREK_BILLMANAGER_ORDER_CONFIG=/etc/spn-ip-failover/xorek-order.json
    export XOREK_BILLMANAGER_ORDER_CONFIG
fi

if [ -f /etc/spn-ip-failover/onecent.json ]; then
    ONECENT_CONFIG=/etc/spn-ip-failover/onecent.json
    export ONECENT_CONFIG
fi

if [ -f /etc/spn-ip-failover/xorek-cart.json ]; then
    XOREK_CART_CONFIG=/etc/spn-ip-failover/xorek-cart.json
    export XOREK_CART_CONFIG
fi

exec "${SPN_PYTHON:-/usr/bin/python3}" -B -m spn_ip_failover "$@"
