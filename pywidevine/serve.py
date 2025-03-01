import base64
import sys
from pathlib import Path
from typing import Optional, Union
from uuid import uuid4, UUID

try:
    from aiohttp import web
except ImportError:
    print(
        "Missing the extra dependencies for serve functionality. "
        "You may install them under poetry with `poetry install -E serve`, "
        "or under pip with `pip install pywidevine[serve]`."
    )
    sys.exit(1)

from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.license_protocol_pb2 import LicenseType, License

routes = web.RouteTableDef()


async def _startup(app: web.Application):
    app["sessions"]: dict[UUID, Cdm] = {}
    app["config"]["devices"] = {
        path.stem: path
        for x in app["config"]["devices"]
        for path in [Path(x)]
    }


async def _cleanup(app: web.Application):
    app["sessions"].clear()
    del app["sessions"]
    app["config"].clear()
    del app["config"]


@routes.get("/")
async def ping(_) -> web.Response:
    return web.json_response({
        "status": 200,
        "message": "Pong!"
    })


@routes.post("/challenge/{license_type}")
async def challenge(request: web.Request) -> web.Response:
    user = request.app["config"]["users"][request.headers["X-Secret-Key"]]
    session_id = uuid4()

    body = await request.json()
    for required_field in ("device_name", "init_data"):
        if not body.get(required_field):
            return web.json_response({
                "status": 400,
                "message": f"Missing required field '{required_field}' in JSON body."
            }, status=400)

    # load device
    device_name = body["device_name"]
    if device_name not in user["devices"] or device_name not in request.app["config"]["devices"]:
        # we don't want to be verbose with the error as to not reveal device names
        # by trial and error to users that are not authorized to use them
        return web.json_response({
            "status": 403,
            "message": f"Device '{device_name}' is not found or you are not authorized to use it."
        }, status=403)
    device = Device.load(request.app["config"]["devices"][device_name])

    # load init data
    init_data = body["init_data"]
    raw = bool(body.get("raw") or 0)

    # load service certificate
    service_certificate = body.get("service_certificate")
    if request.app["config"]["force_privacy_mode"] and not service_certificate:
        return web.json_response({
            "status": 403,
            "message": "No Service Certificate provided but Privacy Mode is Enforced."
        }, status=403)

    # load cdm
    cdm = Cdm(device, init_data, raw)
    if service_certificate:
        cdm.set_service_certificate(service_certificate)
    request.app["sessions"][session_id] = cdm

    # get challenge
    license_request = cdm.get_license_challenge(
        type_=LicenseType.Value(request.match_info["license_type"]),
        privacy_mode=True
    )

    return web.json_response({
        "status": 200,
        "message": "Success",
        "data": {
            "session_id": session_id.hex,
            "challenge_b64": base64.b64encode(license_request).decode()
        }
    }, status=200)


@routes.post("/keys/{key_type}")
async def keys(request: web.Request) -> web.Response:
    body = await request.json()
    for required_field in ("session_id", "license_message"):
        if not body.get(required_field):
            return web.json_response({
                "status": 400,
                "message": f"Missing required field '{required_field}' in JSON body."
            }, status=400)

    # get key type
    key_type = request.match_info["key_type"]
    try:
        if key_type.isdigit():
            key_type = License.KeyContainer.KeyType.Name(int(key_type))
        else:
            License.KeyContainer.KeyType.Value(key_type)  # only test
    except ValueError as e:
        return web.json_response({
            "status": 400,
            "message": f"The Key Type value is invalid, {e}"
        }, status=400)

    # load cdm session
    session_id = UUID(hex=body["session_id"])
    if session_id not in request.app["sessions"]:
        # e.g., app["sessions"] being cleared on server crash, reboot, and such
        # or, the license message was from a challenge that was not made by our Cdm
        return web.json_response({
            "status": 400,
            "message": "Invalid Session ID. Session ID may have Expired."
        }, status=400)
    cdm = request.app["sessions"][session_id]

    # parse the license message
    license_keys = [
        {
            "key_id": key.kid.hex,
            "key": key.key.hex(),
            "type": key.type,
            "permissions": key.permissions,
        }
        for key in cdm.parse_license(body["license_message"])
        if key.type == key_type
    ]

    return web.json_response({
        "status": 200,
        "message": "Success",
        "data": {
            # TODO: Add derived context keys like enc/mac[client]/mac[server]
            "keys": license_keys
        }
    })


@web.middleware
async def authentication(request: web.Request, handler) -> web.Response:
    secret_key = request.headers.get("X-Secret-Key")
    if not secret_key:
        request.app.logger.debug(f"{request.remote} did not provide authorization.")
        return web.json_response({
            "status": "401",
            "message": "Secret Key is Empty."
        }, status=401)

    if secret_key not in request.app["config"]["users"]:
        request.app.logger.debug(f"{request.remote} failed authentication with '{secret_key}'.")
        return web.json_response({
            "status": "401",
            "message": "Secret Key is Invalid, the Key is case-sensitive."
        }, status=401)

    try:
        return await handler(request)
    except web.HTTPException as e:
        request.app.logger.error(f"An unexpected error has occurred, {e}")
        return web.json_response({
            "status": 500,
            "message": e.reason
        }, status=500)


def run(config: dict, host: Optional[Union[str, web.HostSequence]] = None, port: Optional[int] = None):
    app = web.Application(middlewares=[authentication])
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    app.add_routes(routes)
    app["config"] = config
    web.run_app(app, host=host, port=port)
