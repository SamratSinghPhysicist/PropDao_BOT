"""
KCEX Request Signer & Header Generator
======================================
This module implements the reverse-engineered request signing algorithm used by the
KCEX web client (v3.7.91) for private authenticated requests to https://www.kcex.com/fapi/v1.

Reverse-Engineered Logic:
-------------------------
From KCEX web-futures bundle:
    D = Date.now();
    t = Authorization;
    I = md5(t + D).substr(7);
    b = JSON.stringify(body);
    j = md5(D + b + I);
    headers["Content-Sign"] = j;
    headers["Content-time"] = D;
    headers["Authorization"] = t;
    headers["User-Device"] = base64urlEncode(JSON.stringify(device));
"""

import time
import json
import base64
import hashlib
import uuid
from typing import Dict, Any, Optional
from kcex.config import KCEXConfig


class KCEXSigner:
    """
    Handles request signing and header construction for KCEX Futures API.
    """

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
    )

    def __init__(self, config: KCEXConfig):
        self.config = config
        self._default_device_b64: Optional[str] = None

    def get_user_device_header(self) -> str:
        """
        Returns the User-Device header. If provided in config, returns that;
        otherwise generates a stable browser-like device fingerprint.
        """
        if self.config.user_device:
            return self.config.user_device

        if not self._default_device_b64:
            device_info = {
                "visitorId": uuid.uuid4().hex[:16],
                "requestId": uuid.uuid4().hex,
                "brand": "Windows",
                "model": "PC",
                "network": "wifi",
                "isp": "default"
            }
            raw_json = json.dumps(device_info, separators=(',', ':'))
            # base64url encoding (standard base64 without padding or + / replaced)
            encoded = base64.urlsafe_b64encode(raw_json.encode('utf-8')).decode('utf-8').rstrip('=')
            self._default_device_b64 = encoded

        return self._default_device_b64

    def build_public_headers(self) -> Dict[str, str]:
        """
        Headers for public endpoints (ticker, depth, klines, deals, etc.).
        """
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self.config.CLIENT_LANGUAGE,
            "Language": self.config.CLIENT_LANGUAGE,
            "Platform": self.config.CLIENT_PLATFORM,
            "Version": self.config.CLIENT_VERSION,
            "Version-tag": self.config.CLIENT_VERSION_TAG,
            "User-Agent": self.DEFAULT_USER_AGENT,
        }
        return headers

    def sign_request(
        self,
        method: str,
        body: Optional[Any] = None,
        timestamp_ms: Optional[int] = None
    ) -> Dict[str, str]:
        """
        Generates full headers for private authenticated requests, including
        Content-Sign, Content-time, Authorization, and User-Device.

        Args:
            method (str): HTTP method ('GET', 'POST', etc.).
            body (Any): Request payload (dict, list, or string) for POST/PUT.
            timestamp_ms (int, optional): Epoch timestamp in milliseconds.

        Returns:
            Dict[str, str]: Header dictionary ready to attach to requests.Session.
        """
        headers = self.build_public_headers()
        headers["User-Device"] = self.get_user_device_header()

        if self.config.cookie:
            headers["Cookie"] = self.config.cookie

        auth_token = self.config.auth_token
        if auth_token:
            headers["Authorization"] = auth_token

        is_write = method.upper() in ("POST", "PUT")
        if is_write:
            headers["Content-Type"] = "application/json"

        # Content-time and Content-Sign are required on ALL private endpoints (both GET and POST/PUT)
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)

        # Format body as compact JSON string
        if body is None:
            body_str = "" if not is_write else "{}"
        elif isinstance(body, (dict, list)):
            body_str = json.dumps(body, separators=(',', ':'))
        else:
            body_str = str(body)

        # KCEX MD5 signing algorithm:
        # Step 1: I = md5(auth_token + timestamp).substr(7)
        token_and_time = f"{auth_token}{timestamp_ms}"
        hash1 = hashlib.md5(token_and_time.encode('utf-8')).hexdigest()
        intermediate_key = hash1[7:]  # .substr(7) in javascript slices from index 7 to end

        # Step 2: j = md5(timestamp + body_str + intermediate_key)
        sign_input = f"{timestamp_ms}{body_str}{intermediate_key}"
        signature = hashlib.md5(sign_input.encode('utf-8')).hexdigest()

        headers["Content-time"] = str(timestamp_ms)
        headers["Content-Sign"] = signature

        return headers
