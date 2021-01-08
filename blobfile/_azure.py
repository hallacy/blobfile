import binascii
import hashlib
import random
import urllib.parse
import os
import json
import hmac
import base64
import time
import calendar
import datetime
import re
import math
import concurrent.futures
from typing import Any, Mapping, Dict, Optional, Tuple, Sequence, List, Iterator

import xmltodict
import urllib3

from blobfile import _common as common
from blobfile._common import (
    Request,
    Error,
    Stat,
    Context,
    INVALID_HOSTNAME_STATUS,
    TokenManager,
    ConcurrentWriteFailure,
    RequestFailure,
    BaseStreamingReadFile,
    BaseStreamingWriteFile,
    FileBody,
)

SHARED_KEY = "shared_key"
OAUTH_TOKEN = "oauth_token"
ANONYMOUS = "anonymous"

# it looks like azure signed urls cannot exceed the lifetime of the token used
# to create them, so don't keep the key around too long
SAS_TOKEN_EXPIRATION_SECONDS = 60 * 60
# these seem to be expired manually, but we don't currently detect that
SHARED_KEY_EXPIRATION_SECONDS = 24 * 60 * 60

# max 100MB https://docs.microsoft.com/en-us/rest/api/storageservices/put-block#remarks
# there is a preview version of the API that allows this to be 4000MiB
MAX_BLOCK_SIZE = 100_000_000

BLOCK_COUNT_LIMIT = 50_000

RESPONSE_HEADER_TO_REQUEST_HEADER = {
    "Cache-Control": "x-ms-blob-cache-control",
    "Content-Type": "x-ms-blob-content-type",
    "Content-MD5": "x-ms-blob-content-md5",
    "Content-Encoding": "x-ms-blob-content-encoding",
    "Content-Language": "x-ms-blob-content-language",
    "Content-Disposition": "x-ms-blob-content-disposition",
}


def _load_credentials() -> Dict[str, Any]:
    # https://github.com/Azure/azure-sdk-for-python/tree/master/sdk/identity/azure-identity#environment-variables
    # AZURE_STORAGE_KEY seems to be the environment variable mentioned by the az cli
    # AZURE_STORAGE_ACCOUNT_KEY is mentioned elsewhere on the internet
    for varname in ["AZURE_STORAGE_KEY", "AZURE_STORAGE_ACCOUNT_KEY"]:
        if varname in os.environ:
            result = dict(storageAccountKey=os.environ[varname])
            if "AZURE_STORAGE_ACCOUNT" in os.environ:
                result["account"] = os.environ["AZURE_STORAGE_ACCOUNT"]
            return result

    if "AZURE_APPLICATION_CREDENTIALS" in os.environ:
        creds_path = os.environ["AZURE_APPLICATION_CREDENTIALS"]
        if not os.path.exists(creds_path):
            raise Error(
                f"Credentials not found at '{creds_path}' specified by environment variable 'AZURE_APPLICATION_CREDENTIALS'"
            )
        with open(creds_path) as f:
            return json.load(f)

    if "AZURE_CLIENT_ID" in os.environ:
        return dict(
            appId=os.environ["AZURE_CLIENT_ID"],
            password=os.environ["AZURE_CLIENT_SECRET"],
            tenant=os.environ["AZURE_TENANT_ID"],
        )

    if "AZURE_STORAGE_CONNECTION_STRING" in os.environ:
        connection_data = {}
        # technically this should be parsed according to the rules in https://www.connectionstrings.com/formating-rules-for-connection-strings/
        for part in os.environ["AZURE_STORAGE_CONNECTION_STRING"].split(";"):
            key, _, val = part.partition("=")
            connection_data[key.lower()] = val
        return dict(
            account=connection_data["accountname"],
            storageAccountKey=connection_data["accountkey"],
        )

    # look for a refresh token in the az command line credentials
    # https://mikhail.io/2019/07/how-azure-cli-manages-access-tokens/
    default_creds_path = os.path.expanduser("~/.azure/accessTokens.json")
    if os.path.exists(default_creds_path):
        with open(default_creds_path) as f:
            tokens = json.load(f)
            best_token = None
            for token in tokens:
                if best_token is None:
                    best_token = token
                else:
                    # expiresOn may be missing for tokens from service principals
                    if token.get("expiresOn", "") > best_token.get("expiresOn", ""):
                        best_token = token
            if best_token is not None:
                return best_token

    return {}


def load_subscription_ids() -> List[str]:
    """
    Return a list of subscription ids from the local azure profile
    the default subscription will appear first in the list
    """
    default_profile_path = os.path.expanduser("~/.azure/azureProfile.json")
    if not os.path.exists(default_profile_path):
        return []

    with open(default_profile_path, "rb") as f:
        # this file has a UTF-8 BOM
        profile = json.loads(f.read().decode("utf-8-sig"))
    subscriptions = profile["subscriptions"]

    def key_fn(x: Mapping[str, Any]) -> bool:
        return x["isDefault"]

    subscriptions.sort(key=key_fn, reverse=True)
    return [sub["id"] for sub in subscriptions]


def build_url(account: str, template: str, **data: str) -> str:
    return common.build_url(
        f"https://{account}.blob.core.windows.net", template, **data
    )


def _create_access_token_request(
    creds: Mapping[str, str], scope: str, success_codes: Sequence[int] = (200,)
) -> Request:
    if "refreshToken" in creds:
        # https://docs.microsoft.com/en-us/azure/active-directory/develop/v1-protocols-oauth-code#refreshing-the-access-tokens
        data = {
            "grant_type": "refresh_token",
            "refresh_token": creds["refreshToken"],
            "resource": scope,
        }
        tenant = "common"
    else:
        # https://docs.microsoft.com/en-us/azure/active-directory/develop/v1-oauth2-client-creds-grant-flow#request-an-access-token
        # https://docs.microsoft.com/en-us/azure/active-directory/develop/v1-protocols-oauth-code
        # https://docs.microsoft.com/en-us/rest/api/storageservices/authorize-with-azure-active-directory#use-oauth-access-tokens-for-authentication
        # https://docs.microsoft.com/en-us/rest/api/azure/
        # https://docs.microsoft.com/en-us/rest/api/storageservices/authorize-with-azure-active-directory
        # az ad sp create-for-rbac --name <name>
        # az account list
        # az role assignment create --role "Storage Blob Data Contributor" --assignee <appid> --scope "/subscriptions/<account id>"
        data = {
            "grant_type": "client_credentials",
            "client_id": creds["appId"],
            "client_secret": creds["password"],
            "resource": scope,
        }
        tenant = creds["tenant"]
    return Request(
        url=f"https://login.microsoftonline.com/{tenant}/oauth2/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode(data).encode("utf8"),
        success_codes=success_codes,
    )


def create_api_request(req: Request, auth: Tuple[str, str]) -> Request:
    if req.headers is None:
        headers = {}
    else:
        headers = dict(req.headers).copy()

    if req.params is None:
        params = {}
    else:
        params = dict(req.params).copy()

    # https://docs.microsoft.com/en-us/rest/api/storageservices/previous-azure-storage-service-versions
    headers["x-ms-version"] = "2019-02-02"
    headers["x-ms-date"] = datetime.datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
    data = req.data
    if data is not None and isinstance(data, dict):
        data = xmltodict.unparse(data).encode("utf8")

    result = Request(
        method=req.method,
        url=req.url,
        params=params,
        headers=headers,
        data=data,
        preload_content=req.preload_content,
        success_codes=tuple(req.success_codes),
        retry_codes=tuple(req.retry_codes),
    )

    kind, token = auth
    if kind == SHARED_KEY:
        # make sure we are signing the request that has the ms headers added already
        headers["Authorization"] = sign_with_shared_key(result, token)
    elif kind == OAUTH_TOKEN:
        headers["Authorization"] = f"Bearer {token}"
    elif kind == ANONYMOUS:
        pass
    return result


def generate_signed_url(key: Mapping[str, str], url: str) -> Tuple[str, float]:
    # https://docs.microsoft.com/en-us/rest/api/storageservices/delegate-access-with-shared-access-signature
    # https://docs.microsoft.com/en-us/rest/api/storageservices/create-user-delegation-sas
    # https://docs.microsoft.com/en-us/rest/api/storageservices/service-sas-examples
    params = {
        "st": key["SignedStart"],
        "se": key["SignedExpiry"],
        "sks": key["SignedService"],
        "skt": key["SignedStart"],
        "ske": key["SignedExpiry"],
        "sktid": key["SignedTid"],
        "skoid": key["SignedOid"],
        # signed key version (param name not mentioned in docs)
        "skv": key["SignedVersion"],
        "sv": "2018-11-09",  # signed version
        "sr": "b",  # signed resource
        "sp": "r",  # signed permissions
        "sip": "",  # signed ip
        "si": "",  # signed identifier
        "spr": "https,http",  # signed http protocol
        "rscc": "",  # Cache-Control header
        "rscd": "",  # Content-Disposition header
        "rsce": "",  # Content-Encoding header
        "rscl": "",  # Content-Language header
        "rsct": "",  # Content-Type header
    }
    u = urllib.parse.urlparse(url)
    storage_account = u.netloc.split(".")[0]
    canonicalized_resource = urllib.parse.unquote(
        f"/blob/{storage_account}/{u.path[1:]}"
    )
    parts_to_sign = (
        params["sp"],
        params["st"],
        params["se"],
        canonicalized_resource,
        params["skoid"],
        params["sktid"],
        params["skt"],
        params["ske"],
        params["sks"],
        params["skv"],
        params["sip"],
        params["spr"],
        params["sv"],
        params["sr"],
        params["rscc"],
        params["rscd"],
        params["rsce"],
        params["rscl"],
        params["rsct"],
        # this is documented on a different page
        # https://docs.microsoft.com/en-us/rest/api/storageservices/create-service-sas#specifying-the-signed-identifier
        params["si"],
    )
    string_to_sign = "\n".join(parts_to_sign)
    params["sig"] = base64.b64encode(
        hmac.digest(
            base64.b64decode(key["Value"]), string_to_sign.encode("utf8"), "sha256"
        )
    ).decode("utf8")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
    # convert to a utc struct_time by replacing the timezone
    ts = time.strptime(key["SignedExpiry"].replace("Z", "GMT"), "%Y-%m-%dT%H:%M:%S%Z")
    t = calendar.timegm(ts)
    return url + "?" + query, t


def split_path(path: str) -> Tuple[str, str, str]:
    if path.startswith("az://"):
        return split_az_path(path)
    elif path.startswith("https://"):
        return split_https_path(path)
    else:
        raise Error(f"Invalid path: '{path}'")


def split_az_path(path: str) -> Tuple[str, str, str]:
    parts = path[len("az://") :].split("/")
    if len(parts) < 2:
        raise Error(f"Invalid path: '{path}'")
    account = parts[0]
    container = parts[1]
    obj = "/".join(parts[2:])
    return account, container, obj


def split_https_path(path: str) -> Tuple[str, str, str]:
    parts = path[len("https://") :].split("/")
    if len(parts) < 2:
        raise Error(f"Invalid path: '{path}'")
    hostname = parts[0]
    container = parts[1]
    if not hostname.endswith(".blob.core.windows.net") or container == "":
        raise Error(f"Invalid path: '{path}'")
    obj = "/".join(parts[2:])
    account = hostname.split(".")[0]
    return account, container, obj


def combine_https_path(account: str, container: str, obj: str) -> str:
    return f"https://{account}.blob.core.windows.net/{container}/{obj}"


def combine_az_path(account: str, container: str, obj: str) -> str:
    return f"az://{account}/{container}/{obj}"


def combine_path(ctx: Context, account: str, container: str, obj: str) -> str:
    if ctx.output_az_paths:
        return combine_az_path(account, container, obj)
    else:
        return combine_https_path(account, container, obj)


def makedirs(ctx: Context, path: str) -> None:
    """
    Make any directories necessary to ensure that path is a directory
    """
    if not path.endswith("/"):
        path += "/"
    account, container, blob = split_path(path)
    req = Request(
        url=build_url(account, "/{container}/{blob}", container=container, blob=blob),
        method="PUT",
        headers={"x-ms-blob-type": "BlockBlob"},
        success_codes=(201, 400),
    )
    resp = execute_api_request(ctx, req)
    if resp.status == 400:
        raise Error(
            f"Unable to create directory, account/container does not exist: '{path}'"
        )


def sign_with_shared_key(req: Request, key: str) -> str:
    # https://docs.microsoft.com/en-us/rest/api/storageservices/authorize-with-shared-key
    params_to_sign = []
    if req.params is not None:
        for name, value in req.params.items():
            canonical_name = name.lower()
            params_to_sign.append(f"{canonical_name}:{value}")

    u = urllib.parse.urlparse(req.url)
    storage_account = u.netloc.split(".")[0]
    canonical_url = f"/{storage_account}/{u.path[1:]}"
    canonicalized_resource = "\n".join([canonical_url] + list(sorted(params_to_sign)))

    if req.headers is None:
        headers = {}
    else:
        headers = dict(req.headers)

    headers_to_sign = []
    for name, value in headers.items():
        canonical_name = name.lower()
        canonical_value = re.sub(r"\s+", " ", value).strip()
        if canonical_name.startswith("x-ms-"):
            headers_to_sign.append(f"{canonical_name}:{canonical_value}")
    canonicalized_headers = "\n".join(sorted(headers_to_sign))

    content_length = headers.get("Content-Length", "")
    if req.data is not None:
        content_length = str(len(req.data))

    parts_to_sign = [
        req.method,
        headers.get("Content-Encoding", ""),
        headers.get("Content-Language", ""),
        content_length,
        headers.get("Content-MD5", ""),
        headers.get("Content-Type", ""),
        headers.get("Date", ""),
        headers.get("If-Modified-Since", ""),
        headers.get("If-Match", ""),
        headers.get("If-None-Match", ""),
        headers.get("If-Unmodified-Since", ""),
        headers.get("Range", ""),
        canonicalized_headers,
        canonicalized_resource,
    ]
    string_to_sign = "\n".join(parts_to_sign)

    signature = base64.b64encode(
        hmac.digest(base64.b64decode(key), string_to_sign.encode("utf8"), "sha256")
    ).decode("utf8")

    return f"SharedKey {storage_account}:{signature}"


def _get_md5(metadata: Mapping[str, Any]) -> Optional[str]:
    if "Content-MD5" in metadata:
        b64_encoded = metadata["Content-MD5"]
        if b64_encoded is None:
            return None
        return base64.b64decode(b64_encoded).hex()
    else:
        return None


def _parse_timestamp(text: str) -> float:
    return datetime.datetime.strptime(
        text.replace("GMT", "Z"), "%a, %d %b %Y %H:%M:%S %z"
    ).timestamp()


def make_stat(item: Mapping[str, str]) -> Stat:
    if "Creation-Time" in item:
        raw_ctime = item["Creation-Time"]
    else:
        raw_ctime = item["x-ms-creation-time"]
    if "x-ms-meta-blobfilemtime" in item:
        mtime = float(item["x-ms-meta-blobfilemtime"])
    else:
        mtime = _parse_timestamp(item["Last-Modified"])
    return Stat(
        size=int(item["Content-Length"]),
        mtime=mtime,
        ctime=_parse_timestamp(raw_ctime),
        md5=_get_md5(item),
        version=item["Etag"],
    )


def _can_access_container(
    ctx: Context, account: str, container: str, auth: Tuple[str, str]
) -> bool:
    # https://myaccount.blob.core.windows.net/mycontainer?restype=container&comp=list
    success_codes = [200, 403, 404, INVALID_HOSTNAME_STATUS]
    if auth[0] == ANONYMOUS:
        # some containers can produce a 409 error "PublicAccessNotPermitted" when accessed with an anonymous account
        success_codes.append(409)

    def build_req() -> Request:
        req = Request(
            method="GET",
            url=build_url(account, "/{container}", container=container),
            params={"restype": "container", "comp": "list", "maxresults": "1"},
            success_codes=success_codes,
        )
        return create_api_request(req, auth=auth)

    resp = common.execute_request(ctx, build_req)
    # technically INVALID_HOSTNAME_STATUS means we can't access the account because it
    # doesn't exist, but to be consistent with how we treat this error elsewhere we
    # ignore it here
    if resp.status == INVALID_HOSTNAME_STATUS:
        return True
    # anonymous requests will for some reason get a 404 when they should get a 403
    # so treat a 404 from anon requests as a 403
    if resp.status == 404 and auth[0] == ANONYMOUS:
        return False
    # if the container list succeeds or the container doesn't exist, return success
    return resp.status in (200, 404)


def _get_storage_account_id(
    ctx: Context, subscription_id: str, account: str, auth: Tuple[str, str]
) -> Optional[str]:
    # get a list of storage accounts
    def build_req() -> Request:
        req = Request(
            method="GET",
            url=f"https://management.azure.com/subscriptions/{subscription_id}/providers/Microsoft.Storage/storageAccounts",
            params={"api-version": "2019-04-01"},
            success_codes=(200, 401, 403),
        )
        return create_api_request(req, auth=auth)

    resp = common.execute_request(ctx, build_req)
    if resp.status in (401, 403):
        # we aren't allowed to query this for this subscription, skip it
        return None

    out = json.loads(resp.data)
    # check if we found the storage account we are looking for
    for obj in out["value"]:
        if obj["name"] == account:
            return obj["id"]
    return None


def _get_storage_account_key(
    ctx: Context, account: str, container: str, creds: Mapping[str, Any]
) -> Optional[Tuple[Any, float]]:
    # azure resource manager has very low limits on number of requests, so we have
    # to be careful to avoid extra requests here
    # https://docs.microsoft.com/en-us/azure/azure-resource-manager/management/azure-subscription-service-limits#storage-resource-provider-limits

    # in general, this code path should be avoided by using a service principal and
    # giving it access to the bucket

    # get an access token for the management service
    def build_req() -> Request:
        return _create_access_token_request(
            creds=creds, scope="https://management.azure.com/"
        )

    resp = common.execute_request(ctx, build_req)
    result = json.loads(resp.data)
    auth = (OAUTH_TOKEN, result["access_token"])

    # attempt to use list of subscriptions from the azure cli tool
    stored_subscription_ids = load_subscription_ids()

    storage_account_id = None
    for subscription_id in stored_subscription_ids:
        storage_account_id = _get_storage_account_id(
            ctx, subscription_id, account, auth
        )
        if storage_account_id is not None:
            break
    else:
        # if we didn't find the storage account we are looking for, check to see if there
        # are any subscriptions that we did not query
        def build_req() -> Request:
            req = Request(
                method="GET",
                url="https://management.azure.com/subscriptions",
                params={"api-version": "2020-01-01"},
            )
            return create_api_request(req, auth=auth)

        resp = common.execute_request(ctx, build_req)
        result = json.loads(resp.data)
        unchecked_subscription_ids = [
            item["subscriptionId"]
            for item in result["value"]
            if item["subscriptionId"] not in stored_subscription_ids
        ]

        for subscription_id in unchecked_subscription_ids:
            storage_account_id = _get_storage_account_id(
                ctx, subscription_id, account, auth
            )
            if storage_account_id is not None:
                break
        else:
            # we failed to find the storage account, give up
            return None

    def build_req() -> Request:
        req = Request(
            method="POST",
            url=f"https://management.azure.com{storage_account_id}/listKeys",
            params={"api-version": "2019-04-01"},
        )
        return create_api_request(req, auth=auth)

    resp = common.execute_request(ctx, build_req)
    result = json.loads(resp.data)
    for key in result["keys"]:
        if key["permissions"] == "FULL":
            storage_key_auth = (SHARED_KEY, key["value"])
            if _can_access_container(ctx, account, container, storage_key_auth):
                return storage_key_auth
            else:
                raise Error(
                    f"Found storage account key, but it was unable to access storage account: '{account}' and container: '{container}'"
                )
    raise Error(
        f"Storage account was found, but storage account keys were missing: '{account}'"
    )


def _get_access_token(ctx: Context, key: Any) -> Tuple[Any, float]:
    account, container = key
    now = time.time()
    creds = _load_credentials()
    if "storageAccountKey" in creds:
        if "account" in creds:
            if creds["account"] != account:
                raise Error(
                    f"Provided storage account key for account '{creds['account']}' via environment variables, "
                    f"but needed credentials for account '{account}'"
                )
        auth = (SHARED_KEY, creds["storageAccountKey"])
        if _can_access_container(ctx, account, container, auth):
            return (auth, now + SHARED_KEY_EXPIRATION_SECONDS)
    elif "refreshToken" in creds:
        # we have a refresh token, convert it into an access token for this account
        def build_req() -> Request:
            return _create_access_token_request(
                creds=creds,
                scope=f"https://{account}.blob.core.windows.net/",
                success_codes=(200, 400),
            )

        resp = common.execute_request(ctx, build_req)
        result = json.loads(resp.data)
        if resp.status == 400:
            if (
                (
                    result["error"] == "invalid_grant"
                    and "AADSTS700082" in result["error_description"]
                )
                or (
                    result["error"] == "interaction_required"
                    and "AADSTS50078" in result["error_description"]
                )
                or (
                    result["error"] == "interaction_required"
                    and "AADSTS50076" in result["error_description"]
                )
            ):
                raise Error(
                    "Your refresh token is no longer valid, please run `az login` to get a new one"
                )
            else:
                raise Error(
                    f"Encountered an error when requesting an access token: `{result['error']}: {result['error_description']}`.  You can attempt to fix this by re-running `az login`."
                )

        auth = (OAUTH_TOKEN, result["access_token"])

        # for some azure accounts this access token does not work, check if it works
        if _can_access_container(ctx, account, container, auth):
            return (auth, now + float(result["expires_in"]))

        if ctx.use_azure_storage_account_key_fallback:
            # fall back to getting the storage keys
            storage_account_key_auth = _get_storage_account_key(
                ctx=ctx, account=account, container=container, creds=creds
            )
            if storage_account_key_auth is not None:
                return (storage_account_key_auth, now + SHARED_KEY_EXPIRATION_SECONDS)
    elif "appId" in creds:
        # we have a service principal, get an oauth token
        def build_req() -> Request:
            return _create_access_token_request(
                creds=creds, scope="https://storage.azure.com/"
            )

        resp = common.execute_request(ctx, build_req)
        result = json.loads(resp.data)
        auth = (OAUTH_TOKEN, result["access_token"])
        if _can_access_container(ctx, account, container, auth):
            return (auth, now + float(result["expires_in"]))

        if ctx.use_azure_storage_account_key_fallback:
            # fall back to getting the storage keys
            storage_account_key_auth = _get_storage_account_key(
                ctx=ctx, account=account, container=container, creds=creds
            )
            if storage_account_key_auth is not None:
                return (storage_account_key_auth, now + SHARED_KEY_EXPIRATION_SECONDS)

    # oddly, it seems that if you request a public container with a valid azure account, you cannot list the bucket
    # but if you list it with no account, that works fine
    anonymous_auth = (ANONYMOUS, "")
    if _can_access_container(ctx, account, container, anonymous_auth):
        return (anonymous_auth, float("inf"))

    msg = f"Could not find any credentials that grant access to storage account: '{account}' and container: '{container}'"
    if len(creds) == 0:
        msg += """

No Azure credentials were found.  If the container is not marked as public, please do one of the following:

* Log in with 'az login', blobfile will use your default credentials to lookup your storage account key
* Set the environment variable 'AZURE_STORAGE_KEY' to your storage account key which you can find by following this guide: https://docs.microsoft.com/en-us/azure/storage/common/storage-account-keys-manage
* Create an account with 'az ad sp create-for-rbac --name <name>' and set the 'AZURE_APPLICATION_CREDENTIALS' environment variable to the path of the output from that command or individually set the 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', and 'AZURE_TENANT_ID' environment variables"""
    raise Error(msg)


def _get_sas_token(ctx: Context, key: Any) -> Tuple[Any, float]:
    auth = access_token_manager.get_token(ctx, key=key)
    if auth[0] == ANONYMOUS:
        # for public containers, use None as the token so that this will be cached
        # and we can tell when we don't have a real SAS token for a container
        return (None, time.time() + SAS_TOKEN_EXPIRATION_SECONDS)

    account, container = key

    def build_req() -> Request:
        # https://docs.microsoft.com/en-us/rest/api/storageservices/create-user-delegation-sas
        now = datetime.datetime.utcnow()
        start = (now + datetime.timedelta(hours=-1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        expiration = now + datetime.timedelta(days=6)
        expiry = expiration.strftime("%Y-%m-%dT%H:%M:%SZ")
        req = Request(
            url=f"https://{account}.blob.core.windows.net/",
            method="POST",
            params=dict(restype="service", comp="userdelegationkey"),
            data={"KeyInfo": {"Start": start, "Expiry": expiry}},
            success_codes=(200, 403),
        )
        auth = access_token_manager.get_token(ctx, key=key)
        if auth[0] != OAUTH_TOKEN:
            raise Error(
                "Only OAuth tokens can be used to get SAS tokens. You should set the Storage "
                "Blob Data Reader or Storage Blob Data Contributor IAM role. You can run "
                f"`az storage blob list --auth-mode login --account-name {account} --container {container}` "
                "to confirm that the missing role is the issue."
            )
        return create_api_request(req, auth=auth)

    resp = common.execute_request(ctx, build_req)
    if resp.status == 403:
        raise Error(
            f"You do not have permission to generate an SAS token for account {account}. "
            "Try setting the Storage Blob Delegator or Storage Blob Data Contributor IAM role "
            "at the account level."
        )
    out = xmltodict.parse(resp.data)
    t = time.time() + SAS_TOKEN_EXPIRATION_SECONDS
    return out["UserDelegationKey"], t


def execute_api_request(ctx: Context, req: Request) -> urllib3.HTTPResponse:
    u = urllib.parse.urlparse(req.url)
    account = u.netloc.split(".")[0]
    path_parts = u.path.split("/")
    if len(path_parts) < 2:
        raise Error("missing container from path")
    container = u.path.split("/")[1]

    def build_req() -> Request:
        return create_api_request(
            req, auth=access_token_manager.get_token(ctx, key=(account, container))
        )

    return common.execute_request(ctx, build_req)


def _block_index_to_block_id(index: int, upload_id: int) -> str:
    assert index < 2 ** 17
    id_plus_index = (upload_id << 17) + index
    assert id_plus_index < 2 ** 64
    return base64.b64encode(id_plus_index.to_bytes(8, byteorder="big")).decode("utf8")


def _clear_uncommitted_blocks(ctx: Context, url: str, metadata: Dict[str, str]) -> None:
    # to avoid leaking uncommitted blocks, we can do a Put Block List with
    # all the existing blocks for a file
    # this will change the last-modified timestamp and the etag
    req = Request(
        url=url, params=dict(comp="blocklist"), method="GET", success_codes=(200, 404)
    )
    resp = execute_api_request(ctx, req)
    if resp.status != 200:
        return

    result = xmltodict.parse(resp.data)
    if result["BlockList"]["CommittedBlocks"] is None:
        return

    blocks = result["BlockList"]["CommittedBlocks"]["Block"]
    if isinstance(blocks, dict):
        blocks = [blocks]

    body = {"BlockList": {"Latest": [b["Name"] for b in blocks]}}
    # make sure to preserve metadata for the file
    headers: Dict[str, str] = {
        k: v for k, v in metadata.items() if k.startswith("x-ms-meta-")
    }
    for src, dst in RESPONSE_HEADER_TO_REQUEST_HEADER.items():
        if src in metadata:
            headers[dst] = metadata[src]
    req = Request(
        url=url,
        method="PUT",
        params=dict(comp="blocklist"),
        headers={**headers, "If-Match": metadata["etag"]},
        data=body,
        success_codes=(201, 404, 412),
    )
    execute_api_request(ctx, req)


def _finalize_blob(
    ctx: Context, path: str, url: str, block_ids: List[str], md5_digest: bytes
) -> None:
    body = {"BlockList": {"Latest": block_ids}}
    req = Request(
        url=url,
        method="PUT",
        # azure does not calculate md5s for us, we have to do that manually
        # https://blogs.msdn.microsoft.com/windowsazurestorage/2011/02/17/windows-azure-blob-md5-overview/
        headers={"x-ms-blob-content-md5": base64.b64encode(md5_digest).decode("utf8")},
        params=dict(comp="blocklist"),
        data=body,
        success_codes=(201, 400),
    )
    resp = execute_api_request(ctx, req)
    if resp.status == 400:
        result = xmltodict.parse(resp.data)
        if result["Error"]["Code"] == "InvalidBlockList":
            # the most likely way this could happen is if the file was deleted while
            # we were uploading, so assume that is what happened
            # this could be interpreted as a sort of RestartableStreamingWriteFailure but
            # that could result in two processes fighting while uploading the file
            raise ConcurrentWriteFailure.create_from_request_response(
                f"Invalid block list, most likely a concurrent writer wrote to the same path: `{path}`",
                request=req,
                response=resp,
            )
        else:
            raise RequestFailure.create_from_request_response(
                message=f"unexpected status {resp.status}", request=req, response=resp
            )


def isdir(ctx: Context, path: str) -> bool:
    """
    Return true if a path is an existing directory
    """
    if not path.endswith("/"):
        path += "/"
    account, container, blob = split_path(path)
    if blob == "":
        req = Request(
            url=build_url(account, "/{container}", container=container, blob=blob),
            method="GET",
            params=dict(restype="container"),
            success_codes=(200, 404, INVALID_HOSTNAME_STATUS),
        )
        resp = execute_api_request(ctx, req)
        return resp.status == 200
    else:
        # even though we're only interested in having one result, we still need to make an
        # iterator. as it happens, azure is perfectly willing to return an empty first page.
        it = create_page_iterator(
            ctx,
            url=build_url(account, "/{container}", container=container),
            method="GET",
            params=dict(
                comp="list",
                restype="container",
                prefix=blob,
                delimiter="/",
                maxresults="1",
            ),
        )
        for result in it:
            if result["Blobs"] is not None:
                return "BlobPrefix" in result["Blobs"] or "Blob" in result["Blobs"]
        return False


def create_page_iterator(
    ctx: Context,
    url: str,
    method: str,
    data: Optional[Mapping[str, str]] = None,
    params: Optional[Mapping[str, str]] = None,
) -> Iterator[Dict[str, Any]]:
    if params is None:
        p = {}
    else:
        p = dict(params).copy()
    if data is None:
        d = None
    else:
        d = dict(data).copy()
    while True:
        req = Request(
            url=url,
            method=method,
            params=p,
            data=d,
            success_codes=(200, 404, INVALID_HOSTNAME_STATUS),
        )
        resp = execute_api_request(ctx, req)
        if resp.status in (404, INVALID_HOSTNAME_STATUS):
            return
        result = xmltodict.parse(resp.data)["EnumerationResults"]
        yield result
        if result["NextMarker"] is None:
            break
        p["marker"] = result["NextMarker"]


class StreamingReadFile(BaseStreamingReadFile):
    def __init__(self, ctx: Context, path: str) -> None:
        st = maybe_stat(ctx, path)
        if st is None:
            raise FileNotFoundError(f"No such file or directory: '{path}'")
        super().__init__(ctx=ctx, path=path, size=st.size)

    def _request_chunk(
        self, streaming: bool, start: int, end: Optional[int] = None
    ) -> urllib3.response.HTTPResponse:
        account, container, blob = split_path(self._path)
        req = Request(
            url=build_url(
                account, "/{container}/{blob}", container=container, blob=blob
            ),
            method="GET",
            headers={"Range": common.calc_range(start=start, end=end)},
            success_codes=(206, 416),
            # if we are streaming the data, make
            # sure we don't preload it
            preload_content=not streaming,
        )
        resp = execute_api_request(self._ctx, req)
        return resp


class StreamingWriteFile(BaseStreamingWriteFile):
    def __init__(self, ctx: Context, path: str) -> None:
        self._path = path
        account, container, blob = split_path(path)
        self._url = build_url(
            account, "/{container}/{blob}", container=container, blob=blob
        )
        # block blobs let you upload up to 100,000 "uncommitted" blocks with user-chosen block ids
        # using the "Put Block" call
        # you may then call "Put Block List" with up to 50,000 block ids of the blocks you
        # want to be in the blob (50,000 is the max blocks per blob)
        # all unused uncommitted blocks will be deleted
        # uncommitted blocks also expire after a week if they are not committed
        #
        # since we use block blobs, there are a few ways we could implement this streaming write file
        #
        # method 1:
        #   upload the first chunk of the file as block id "0", the second as block id "1" etc
        #   when we are done writing the file, we call "Put Block List" using range(num_blocks) as
        #   the block ids
        #
        #   this has the advantage that if our program crashes, the same block ids will be reused
        #   for the next upload and so we'll never get more than 50,000 uncommitted blocks
        #
        #   in general, azure does not seem to support concurrent writers except maybe
        #   for writing small files (GCS does to a limited extent through resumable upload sessions)
        #
        #   with method 1, if you have two writers:
        #
        #       writer 0: write block id "0"
        #       writer 1: write block id "0"
        #       writer 1: crash
        #       writer 0: write block id "1"
        #       writer 0: put block list ["0", "1"]
        #
        #   then you will end up with block "0" from writer 1 and block "1" from writer 0, which means
        #   your file will be corrupted
        #
        #   this appears to be the method used by the azure python SDK
        #
        # method 2:
        #   generate a random session id
        #   upload the first chunk of the file as block id "<session id>-0",
        #       the second block as "<session id>-1" etc
        #   when we are done writing the file, call "Put Block List" using
        #       [f"<session id>-{i}" for i in range(num_blocks)] as the block list
        #
        #   this has the advantage that we should not observe data corruption from concurrent writers
        #       assuming that the session ids are unique, although whichever writer finishes first will
        #       win, because calling "Put Block List" will delete all uncommitted blocks
        #
        #   this has the disadvantage that we can end up hitting the uncommitted block limit
        #       1) with 100,000 concurrent writers, each one would write the first block, then all
        #           would immediately hit the block limit and get 409 errors
        #       2) with a single writer that crashes every time it writes the second block, it would
        #           retry 100,000 times, then be unable to continue due to all the uncommitted blocks
        #           it was generating
        #
        #   the workaround we use here is that whenever a file is opened for reading, we clear all
        #       uncommitted blocks by calling "Put Block List" with the list of currently committed blocks
        #
        #   this seems to be reasonably fast in practice, and means that failure #2 should not be an issue
        #
        #   failure #1 could still happen with concurrent writers, but this should result only in a
        #       confusing error message (409 error) instead of a ConcurrentWriteFailure, though we
        #       could likely raise that error if we saw a 409 with the error RequestEntityTooLargeBlockCountExceedsLimit
        #
        #   this does change the behavior slightly, now the writer that will end up succeeding on "Put Block List"
        #       is likely to be the last writer to open the file for writing, the others will fail
        #       because their uncommitted blocks have been cleared
        #
        # it would be nice to replace this with a less odd method, but it's not obvious how
        #   to do this on azure storage
        #
        # if there were upload sessions like GCS, this wouldn't be an issue
        # if there was no uncommitted block limit, method 2 would work fine
        # if blobs could automatically expire without having to add a container lifecycle rule
        #   then we could upload to a temp path, then copy to the final path (assuming copy is atomic)
        #   without automatic expiry, we'd leak temp files
        # we can use the lease system, but then we have to deal with leases

        self._upload_id = random.randint(0, 2 ** 47 - 1)
        self._block_index = 0
        # check to see if there is an existing blob at this location with the wrong type
        req = Request(
            url=self._url,
            method="HEAD",
            success_codes=(200, 400, 404, INVALID_HOSTNAME_STATUS),
        )
        resp = execute_api_request(ctx, req)
        if resp.status == 200:
            if resp.headers["x-ms-blob-type"] == "BlockBlob":
                # because we delete all the uncommitted blocks, any concurrent writers will fail
                # but they would fail anyway since the first writer to finish would end up
                # deleting all uncommitted blocks
                # this means that the last writer to start is likely to win, the others should fail
                # with ConcurrentWriteFailure
                _clear_uncommitted_blocks(ctx, self._url, resp.headers)
            else:
                # if the existing blob type is not compatible with the block blob we are about to write
                # we have to delete the file before writing our block blob or else we will get a 409
                # error when putting the first block
                remove(ctx, path)
        elif resp.status in (400, INVALID_HOSTNAME_STATUS) or (
            resp.status == 404
            and resp.headers["x-ms-error-code"] == "ContainerNotFound"
        ):
            raise FileNotFoundError(
                f"No such file or container/account does not exist: '{path}'"
            )
        self._md5 = hashlib.md5()
        super().__init__(ctx=ctx, chunk_size=ctx.azure_write_chunk_size)

    def _upload_chunk(self, chunk: bytes, finalize: bool) -> None:
        start = 0
        while start < len(chunk):
            # premium block blob storage supports block blobs and append blobs
            # https://azure.microsoft.com/en-us/blog/azure-premium-block-blob-storage-is-now-generally-available/
            # we use block blobs because they are compatible with WASB:
            # https://docs.microsoft.com/en-us/azure/databricks/kb/data-sources/wasb-check-blob-types
            end = start + self._ctx.azure_write_chunk_size
            data = chunk[start:end]
            self._md5.update(data)
            req = Request(
                url=self._url,
                method="PUT",
                params=dict(
                    comp="block",
                    blockid=_block_index_to_block_id(
                        self._block_index, self._upload_id
                    ),
                ),
                data=data,
                success_codes=(201,),
            )
            execute_api_request(self._ctx, req)
            self._block_index += 1
            if self._block_index >= BLOCK_COUNT_LIMIT:
                raise Error(
                    f"Exceeded block count limit of {BLOCK_COUNT_LIMIT} for Azure Storage.  Increase `azure_write_chunk_size` so that {BLOCK_COUNT_LIMIT} * `azure_write_chunk_size` exceeds the size of the file you are writing."
                )

            start += self._ctx.azure_write_chunk_size

        if finalize:
            block_ids = [
                _block_index_to_block_id(i, self._upload_id)
                for i in range(self._block_index)
            ]
            _finalize_blob(
                ctx=self._ctx,
                path=self._path,
                url=self._url,
                block_ids=block_ids,
                md5_digest=self._md5.digest(),
            )


def _upload_chunk(
    ctx: Context, path: str, start: int, size: int, url: str, block_id: str
) -> None:
    req = Request(
        url=url,
        method="PUT",
        params=dict(comp="block", blockid=block_id),
        # this needs to be specified since we use a file object for the data
        headers={"Content-Length": str(size)},
        data=FileBody(path, start=start, end=start + size),
        success_codes=(201,),
    )
    execute_api_request(ctx, req)


def parallel_upload(
    ctx: Context,
    executor: concurrent.futures.Executor,
    src: str,
    dst: str,
    return_md5: bool,
) -> Optional[str]:
    with open(src, "rb") as f:
        md5_digest = common.block_md5(f)

    account, container, blob = split_path(dst)
    dst_url = build_url(account, "/{container}/{blob}", container=container, blob=blob)

    upload_id = random.randint(0, 2 ** 47 - 1)
    s = os.stat(src)
    block_ids = []
    max_workers = getattr(executor, "_max_workers", os.cpu_count() or 1)
    part_size = min(
        max(math.ceil(s.st_size / max_workers), common.PARALLEL_COPY_MINIMUM_PART_SIZE),
        MAX_BLOCK_SIZE,
    )
    i = 0
    start = 0
    futures = []
    while start < s.st_size:
        block_id = _block_index_to_block_id(i, upload_id)
        future = executor.submit(
            _upload_chunk,
            ctx,
            src,
            start,
            min(ctx.azure_write_chunk_size, s.st_size - start),
            dst_url,
            block_id,
        )
        futures.append(future)
        block_ids.append(block_id)
        i += 1
        start += part_size
    for future in futures:
        future.result()

    _finalize_blob(
        ctx=ctx, path=dst, url=dst_url, block_ids=block_ids, md5_digest=md5_digest
    )
    return binascii.hexlify(md5_digest).decode("utf8") if return_md5 else None


def maybe_stat(ctx: Context, path: str) -> Optional[Stat]:
    account, container, blob = split_path(path)
    if blob == "":
        return None
    req = Request(
        url=build_url(account, "/{container}/{blob}", container=container, blob=blob),
        method="HEAD",
        success_codes=(200, 404, INVALID_HOSTNAME_STATUS),
    )
    resp = execute_api_request(ctx, req)
    if resp.status != 200:
        return None
    return make_stat(resp.headers)


def remove(ctx: Context, path: str) -> bool:
    account, container, blob = split_path(path)
    if blob == "":
        raise FileNotFoundError(f"The system cannot find the path specified: '{path}'")
    req = Request(
        url=build_url(account, "/{container}/{blob}", container=container, blob=blob),
        method="DELETE",
        success_codes=(202, 404, INVALID_HOSTNAME_STATUS),
    )
    resp = execute_api_request(ctx, req)
    return resp.status == 202


def maybe_update_md5(ctx: Context, path: str, etag: str, hexdigest: str) -> bool:
    account, container, blob = split_path(path)
    req = Request(
        url=build_url(account, "/{container}/{blob}", container=container, blob=blob),
        method="HEAD",
        headers={"If-Match": etag},
        success_codes=(200, 404, 412),
    )
    resp = execute_api_request(ctx, req)
    if resp.status in (404, 412):
        return False

    # these will be cleared if not provided, there does not appear to be a PATCH method like for GCS
    # https://docs.microsoft.com/en-us/rest/api/storageservices/set-blob-properties#remarks
    headers: Dict[str, str] = {}
    for src, dst in RESPONSE_HEADER_TO_REQUEST_HEADER.items():
        if src in resp.headers:
            headers[dst] = resp.headers[src]
    headers["x-ms-blob-content-md5"] = base64.b64encode(
        binascii.unhexlify(hexdigest)
    ).decode("utf8")

    req = Request(
        url=build_url(account, "/{container}/{blob}", container=container, blob=blob),
        method="PUT",
        params=dict(comp="properties"),
        headers={
            **headers,
            # https://docs.microsoft.com/en-us/rest/api/storageservices/specifying-conditional-headers-for-blob-service-operations
            "If-Match": etag,
        },
        success_codes=(200, 404, 412),
    )
    resp = execute_api_request(ctx, req)
    return resp.status == 200


access_token_manager = TokenManager(_get_access_token)

sas_token_manager = TokenManager(_get_sas_token)