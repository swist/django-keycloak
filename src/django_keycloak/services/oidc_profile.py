from datetime import timedelta

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from django_keycloak.models import OpenIdConnectProfile
from django_keycloak.services.exceptions import TokensExpired

import django_keycloak.services.realm

logger = logging.getLogger(__name__)


def get_or_create_from_id_token(client, id_token):
    """
    Get or create OpenID Connect profile from given id_token.

    :param django_keycloak.models.Client client:
    :param str id_token:
    :rtype: django_keycloak.models.OpenIdConnectProfile
    """
    issuer = django_keycloak.services.realm.get_issuer(client.realm)

    id_token_object = client.openid_api_client.decode_token(
        token=id_token,
        key=client.realm.certs,
        algorithms=client.openid_api_client.well_known[
            'id_token_signing_alg_values_supported'],
        issuer=issuer
    )

    with transaction.atomic():
        UserModel = get_user_model()
        email_field_name = UserModel.get_email_field_name()
        user, _ = UserModel.objects.update_or_create(
            username=id_token_object['sub'],
            defaults={
                email_field_name: id_token_object.get('email', ''),
                'first_name': id_token_object.get('given_name', ''),
                'last_name': id_token_object.get('family_name', '')
            }
        )

        oidc_profile, _ = OpenIdConnectProfile.objects.update_or_create(
            sub=id_token_object['sub'],
            defaults={
                'realm': client.realm,
                'user': user
            }
        )

    return oidc_profile


def update_or_create_from_code(code, client, redirect_uri):
    """
    Update or create an user based on an authentication code.
    Response as specified in:

    https://tools.ietf.org/html/rfc6749#section-4.1.4

    :param django_keycloak.models.Client client:
    :param str code: authentication code
    :param str redirect_uri
    :rtype: django_keycloak.models.OpenIdConnectProfile
    """

    # Define "initiate_time" before getting the access token to calculate
    # before which time it expires.
    initiate_time = timezone.now()
    token_response = client.openid_api_client.authorization_code(
        code=code, redirect_uri=redirect_uri)

    return _update_or_create(client=client, token_response=token_response,
                             initiate_time=initiate_time)


def _update_or_create(client, token_response, initiate_time):
    """
    Update or create an user based on a token response.

    `token_response` contains the items returned by the OpenIDConnect Token API
    end-point:
     - id_token
     - access_token
     - expires_in
     - refresh_token
     - refresh_expires_in

    :param django_keycloak.models.Client client:
    :param dict token_response:
    :param datetime.datetime initiate_time:
    :rtype: django_keycloak.models.OpenIdConnectProfile
    """
    issuer = django_keycloak.services.realm.get_issuer(client.realm)

    id_token_object = client.openid_api_client.decode_token(
        token=token_response['id_token'],
        key=client.realm.certs,
        algorithms=client.openid_api_client.well_known[
            'id_token_signing_alg_values_supported'],
        issuer=issuer
    )

    userinfo = client.openid_api_client.userinfo(
        token=token_response['access_token'])

    with transaction.atomic():
        UserModel = get_user_model()
        email_field_name = UserModel.get_email_field_name()
        user, _ = UserModel.objects.update_or_create(
            username=id_token_object['sub'],
            defaults={
                email_field_name: userinfo.get('email', ''),
                'first_name': userinfo.get('given_name', ''),
                'last_name': userinfo.get('family_name', '')
            }
        )

        oidc_profile, _ = OpenIdConnectProfile.objects.update_or_create(
            sub=id_token_object['sub'],
            defaults={
                'realm': client.realm,
                'user': user
            }
        )

    return update_tokens(token_model=oidc_profile,
                         token_response=token_response,
                         initiate_time=initiate_time)


def update_tokens(token_model, token_response, initiate_time):
    """
    Update tokens on the OpenID Connect profile

    :param django_keycloak.models.TokenModelAbstract token_model:
    :param dict token_response: response from OIDC token API end-point
    :param datetime.datetime initiate_time: timestamp before the token request
    :rtype: django_keycloak.models.OpenIdConnectProfile
    """
    expires_before = initiate_time + timedelta(
        seconds=token_response['expires_in'])
    refresh_expires_before = initiate_time + timedelta(
        seconds=token_response['refresh_expires_in'])

    token_model.access_token = token_response['access_token']
    token_model.expires_before = expires_before
    token_model.refresh_token = token_response['refresh_token']
    token_model.refresh_expires_before = refresh_expires_before

    token_model.save(update_fields=['access_token',
                                    'expires_before',
                                    'refresh_token',
                                    'refresh_expires_before'])
    return token_model


def get_active_access_token(oidc_profile):
    """
    Give access_token and refresh when required.

    :param django_keycloak.models.KeycloakOpenIDProfile openid_profile:
    :rtype: string
    :raise: django_keycloak.services.exceptions.TokensExpired
    """
    initiate_time = timezone.now()

    if oidc_profile.refresh_expires_before is None \
            or initiate_time > oidc_profile.refresh_expires_before:
        raise TokensExpired()

    if initiate_time > oidc_profile.expires_before:
        # Refresh token
        token_response = oidc_profile.realm.client.openid_api_client\
            .refresh_token(refresh_token=oidc_profile.refresh_token)

        oidc_profile = update_tokens(token_model=oidc_profile,
                                     token_response=token_response,
                                     initiate_time=initiate_time)

    return oidc_profile.access_token


def get_entitlement(oidc_profile):
    """
    Get entitlement.

    http://www.keycloak.org/docs/latest/authorization_services/index.html#_service_entitlement_api

    :param django_keycloak.models.KeycloakOpenIDProfile oidc_profile:
    :rtype: dict
    :return: Decoded RPT
    """
    access_token = get_active_access_token(oidc_profile=oidc_profile)

    rpt = oidc_profile.realm.client.authz_api_client.entitlement(
        token=access_token)

    rpt_decoded = oidc_profile.realm.client.openid_api_client.decode_token(
        token=rpt['rpt'],
        key=oidc_profile.realm.certs,
        options={
            'verify_signature': True,
            'exp': True,
            'iat': True,
            'aud': True
        })
    return rpt_decoded
