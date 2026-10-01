"""Firebase email/password sign-in -- gates only the Controls &
Settings page (every other page is read-only and exposes nothing
secret). Flow: REST sign-in for an ID token, firebase-admin verifies
it server-side, then the token's email must be in `allowed_emails`
(core/config.py) -- an explicit allowlist, not "any Firebase user",
matching AI-Trader's dashboard auth precedent.
"""

from __future__ import annotations

import json

import requests
import streamlit as st

from core.config import settings

_SESSION_KEY = "survivor_authed_email"


def _firebase_app():
    import firebase_admin
    from firebase_admin import credentials

    if not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(settings.firebase_service_account_json))
        firebase_admin.initialize_app(cred)
    return firebase_admin.get_app()


def current_email() -> str | None:
    return st.session_state.get(_SESSION_KEY)


def sign_out() -> None:
    st.session_state.pop(_SESSION_KEY, None)


def _sign_in_with_password(email: str, password: str) -> str:
    resp = requests.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": settings.firebase_web_api_key},
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["idToken"]


def require_login() -> str:
    """Renders a sign-in form and halts the page until a verified,
    allow-listed email is in session_state; returns that email once
    logged in."""
    email = current_email()
    if email:
        return email

    st.title("Sign in")
    with st.form("sign_in"):
        email_input = st.text_input("Email")
        password_input = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        try:
            id_token = _sign_in_with_password(email_input, password_input)
            _firebase_app()
            from firebase_admin import auth as firebase_auth

            decoded = firebase_auth.verify_id_token(id_token)
            verified_email = decoded.get("email", "").lower()
            if verified_email not in settings.allowed_emails_list:
                st.error("This account isn't allowed to access Controls & Settings.")
            else:
                st.session_state[_SESSION_KEY] = verified_email
                st.rerun()
        except requests.HTTPError:
            st.error("Wrong email or password.")
        except Exception as exc:  # noqa: BLE001 -- bad service account JSON etc. surfaces as a login error, not a crash
            st.error(f"Sign-in failed: {exc}")

    st.stop()
