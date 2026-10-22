"""Constants for SP Group mobile API clients."""

from __future__ import annotations

AUTH0_BASE_URL = "https://identity.spdigital.sg"
MULESOFT_AUTH_BASE_URL = "https://b2c.api.spdigital.sg"
MULESOFT_PUBLIC_BASE_URL = "https://public.api.spdigital.sg"
JARVIS_BASE_URL = MULESOFT_AUTH_BASE_URL
JARVIS_PUBLIC_BASE_URL = MULESOFT_PUBLIC_BASE_URL

AUTH0_CLIENT_ID = "z1tl6I1V6HI201ule9tmSALb97hw8Biu"
AUTH0_AUDIENCE = "https://profile.up.spdigital.sg/"
AUTH0_REALM = "Username-Password-Authentication"
AUTH0_CLIENT_HEADER = "eyJuYW1lIjoiYXV0aDAtYW5kcm9pZCIsInZlcnNpb24iOiIxLjEyLjAifQ=="

PASSWORD_REALM_GRANT_TYPE = "http://auth0.com/oauth/grant-type/password-realm"
MFA_OOB_GRANT_TYPE = "http://auth0.com/oauth/grant-type/mfa-oob"
MFA_OTP_GRANT_TYPE = "http://auth0.com/oauth/grant-type/mfa-otp"
REFRESH_TOKEN_GRANT_TYPE = "refresh_token"
TOKEN_EXPIRY_BUFFER_SECONDS = 60

DEFAULT_SCOPE = (
    "openid email profile offline_access user_metadata enroll "
    "read:authenticators me:rbac me me:uportal"
)
APK_SCOPE = "openid profile email user_metadata offline_access"

APK_USER_AGENT = "Infinity/15.9.0 (Android)"
PROTOTYPE_USER_AGENT = "SP-Services-Android/15.9.0 okhttp/4.12.0"

AUTH0_JSON_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept-Language": "en_US",
    "Auth0-Client": AUTH0_CLIENT_HEADER,
}

AUTH0_GET_JSON_HEADERS = {
    "Content-Type": "application/json",
}

JARVIS_ACCEPT = "application/json, */*"

AMI_GROUP_HOURLY = "day"
AMI_GROUP_WEEKLY = "week"
AMI_GROUP_DAILY = "month"
AMI_GROUP_YEAR = "year"

AMI_GROUPS = frozenset(
    {AMI_GROUP_HOURLY, AMI_GROUP_WEEKLY, AMI_GROUP_DAILY, AMI_GROUP_YEAR}
)
UTILITY_TYPES = frozenset({"electric", "water", "gas"})
