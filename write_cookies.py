#!/usr/bin/env python3
"""Write TV cookies to storage file."""

import json
from pathlib import Path

state_path = Path("i:/1H/data/tv_storage_state.json")
state_path.parent.mkdir(parents=True, exist_ok=True)

cookies_data = {
    "cookies": [
        {
            "name": "_sp_ses.cf1a",
            "value": "*",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "cookiePrivacyPreferenceBannerProduction",
            "value": "notApplicable",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "_ga",
            "value": "GA1.1.1391283774.1782328707",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "cookiesSettings",
            "value": '{"analytics":true,"advertising":true}',
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "_gcl_au",
            "value": "1.1.1588031575.1782328709",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "g_state",
            "value": '{"i_l":0,"i_ll":1782328708838,"i_b":"J20DiFNNhKM/P3BAQbHXicau4tUQAPupWiZCmNRjGHw","i_e":{"enable_itp_optimization":24},"i_et":1782328708838}',
            "domain": "www.tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "sessionid",
            "value": "cs63e5e13am1fu7a5q7fpt90u30mwixg",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "sessionid_sign",
            "value": "v3:EDa7rwJXF+2bMTbd+ux9wAYVnrdU1Z6tQBFdDN42xSA=",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "device_t",
            "value": "aDc1Z0NBOjE._GyDML0qrU0o6gS-XLiGLNj17MPccPJDcnzhmOrxuUQ",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        },
        {
            "name": "png",
            "value": "550fa9ae-1c00-4d8f-9737-a957480aa5fa",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "etg",
            "value": "550fa9ae-1c00-4d8f-9737-a957480aa5fa",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "cachec",
            "value": "550fa9ae-1c00-4d8f-9737-a957480aa5fa",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "tv_ecuid",
            "value": "550fa9ae-1c00-4d8f-9737-a957480aa5fa",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "_ga_YVVRYGL0E0",
            "value": "GS2.1.s1782328707$o1$g1$t1782329350$j60$l0$h0",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "_sp_id.cf1a",
            "value": "eae51bee-3482-489e-b394-f47926697dc5.1782328706.1.1782329356..4e266a2d-1ed7-4efa-8c25-8faeaab966d0..d4ed8253-d14e-4b64-b449-620c341bd37c.1782328707947.10",
            "domain": ".tradingview.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        },
    ],
    "origins": [],
}

state_path.write_text(json.dumps(cookies_data, indent=2))
print(f"✅ Saved {len(cookies_data['cookies'])} cookies to {state_path}")
