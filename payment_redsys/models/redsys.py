# Copyright 2016-2017 Tecnativa - Sergio Teruel
# Copyright 2019 Ignacio Ibeas <ignacio@acysos.com>
# Copyright 2020 Tecnativa - João Marques
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import hashlib
import hmac

import json
import logging
import urllib
from odoo.tools import config
from odoo import _, api, exceptions, fields, http, models


_logger = logging.getLogger(__name__)

try:
    from Crypto.Cipher import DES3
except ImportError:
    _logger.info("Missing dependency (pycryptodome). See README.")


class AcquirerRedsys(models.Model):
    _inherit = "payment.acquirer"

    def _get_redsys_urls(self, environment):
        """ Redsys URLs
        """
        if environment == "prod":
            return {
                "redsys_form_url": "https://sis.redsys.es/sis/realizarPago/",
            }
        else:
            return {
                "redsys_form_url": "https://sis-t.redsys.es:25443/sis/realizarPago/",
            }

    def _get_redsys_url_s2s(self):
        if self.state == "enabled":
            return 'https://sis.redsys.es/sis/rest/trataPeticionREST'
        else:
            return 'https://sis-t.redsys.es:25443/sis/rest/trataPeticionREST'

    provider = fields.Selection(selection_add=[("redsys", "Redsys")],
                                ondelete={'redsys': 'set default', 'none': 'set default'})
    redsys_merchant_name = fields.Char("Merchant Name", required_if_provider="redsys")
    redsys_merchant_code = fields.Char("Merchant code", required_if_provider="redsys")
    redsys_merchant_description = fields.Char(
        "Product Description", required_if_provider="redsys"
    )
    redsys_secret_key = fields.Char("Secret Key", required_if_provider="redsys")
    redsys_terminal = fields.Char(
        "Terminal", default="1", required_if_provider="redsys"
    )
    redsys_currency = fields.Char(
        "Currency", default="978", required_if_provider="redsys"
    )
    redsys_transaction_type = fields.Char(
        "Transaction Type", default="0", required_if_provider="redsys"
    )
    redsys_merchant_data = fields.Char("Merchant Data")
    redsys_merchant_lang = fields.Selection(
        [
            ("001", "Castellano"),
            ("002", "Inglés"),
            ("003", "Catalán"),
            ("004", "Francés"),
            ("005", "Alemán"),
            ("006", "Holandés"),
            ("007", "Italiano"),
            ("008", "Sueco"),
            ("009", "Portugués"),
            ("010", "Valenciano"),
            ("011", "Polaco"),
            ("012", "Gallego"),
            ("013", "Euskera"),
        ],
        "Merchant Consumer Language",
        default="001",
    )
    redsys_pay_method = fields.Selection(
        [
            ("T", "Pago con Tarjeta"),
            ("R", "Pago por Transferencia"),
            ("D", "Domiciliacion"),
        ],
        "Payment Method",
        default="T",
    )
    redsys_signature_version = fields.Selection(
        [("HMAC_SHA256_V1", "HMAC SHA256 V1")], default="HMAC_SHA256_V1"
    )
    send_quotation = fields.Boolean("Send quotation", default=True)
    redsys_percent_partial = fields.Float(
        string="Reduction percent",
        digits="Account",
        help="Write percent reduction payment, for this method payment."
             "With this option you can allow partial payments in your "
             "shop online, the residual amount in pending for do a manual "
             "payment later.",
    )

    @api.constrains("redsys_percent_partial")
    def check_redsys_percent_partial(self):
        if self.redsys_percent_partial < 0 or self.redsys_percent_partial > 100:
            raise exceptions.Warning(
                _("Partial payment percent must be between 0 and 100")
            )

    @api.model
    def _get_website_callback_url(self):
        """For force a callback url from Redsys distinct to base url website,
         only apply to a Redsys response.
        """
        get_param = self.env["ir.config_parameter"].sudo().get_param
        return get_param("payment_redsys.callback_url")

    @api.model
    def _get_website_url(self):
        """
        For a single website setting the domain website name is not accesible
        for the user, by default is localhost so the system get domain from
        system parameters instead of domain of website record.
        """
        if config["test_enable"]:
            return self.env["ir.config_parameter"].sudo().get_param("web.base.url")

        domain = False
        # _logger.info("=========%r",[http.request])
        if hasattr(http.request, 'website'):
            domain = http.request.website.domain

        if domain:
            base_url = http.request.website.domain
        else:
            base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return base_url or ""

    def _prepare_merchant_parameters(self, tx_values, recurring=True):
        # Check multi-website
        base_url = self._get_website_url()
        callback_url = self._get_website_callback_url()
        if self.redsys_percent_partial > 0:
            amount = tx_values["amount"]
            tx_values["amount"] = amount - (amount * self.redsys_percent_partial / 100)
        values = {
            "Ds_Sermepa_Url": self.redsys_get_form_action_url(),
            "Ds_Merchant_Amount": str(int(round(tx_values["amount"] * 100))),
            "Ds_Merchant_Currency": self.redsys_currency or "978",
            "Ds_Merchant_Order": (
                    tx_values["reference"] and tx_values["reference"][-12:] or False
            ),
            "Ds_Merchant_MerchantCode": (
                    self.redsys_merchant_code and self.redsys_merchant_code[:9]
            ),
            "Ds_Merchant_Terminal": self.redsys_terminal or "1",
            "Ds_Merchant_TransactionType": (self.redsys_transaction_type or "0"),
            # "DS_MERCHANT_COF_INI": "S",
            # "DS_MERCHANT_COF_TYPE": "R",
            # "DS_MERCHANT_IDENTIFIER": "REQUIRED",
            "Ds_Merchant_Titular": tx_values.get(
                "billing_partner", self.env.user.partner_id
            ).display_name[:60],
            "Ds_Merchant_MerchantName": (
                    self.redsys_merchant_name and self.redsys_merchant_name[:25]
            ),
            "Ds_Merchant_MerchantUrl": ("%s/payment/redsys/return" % (callback_url or base_url))[:250],
            "Ds_Merchant_MerchantData": self.redsys_merchant_data or "",
            "Ds_Merchant_ProductDescription": (self._product_description(tx_values["reference"])
                                               or self.redsys_merchant_description
                                               and self.redsys_merchant_description[:125]),
            "Ds_Merchant_ConsumerLanguage": (self.redsys_merchant_lang or "001"),
            "Ds_Merchant_UrlOk": "%s/payment/redsys/result/redsys_result_ok" % (callback_url or base_url),
            "Ds_Merchant_UrlKo": "%s/payment/redsys/result/redsys_result_ko" % (callback_url or base_url),
            "Ds_Merchant_Paymethods": self.redsys_pay_method or "T",
        }
        _logger.info("=======%r", values)
        return self._url_encode64(json.dumps(values))

    def _url_encode64(self, data):
        data = base64.b64encode(data.encode())
        return data

    def _url_decode64(self, data):
        return json.loads(base64.b64decode(data).decode())

    def sign_parameters(self, secret_key, params64):
        params_dic = self._url_decode64(params64)
        if "Ds_Merchant_Order" in params_dic:
            order = str(params_dic["Ds_Merchant_Order"])
        else:
            order = str(urllib.parse.unquote(params_dic.get("Ds_Order", "Not found")))
        cipher = DES3.new(
            key=base64.b64decode(secret_key), mode=DES3.MODE_CBC, IV=b"\0\0\0\0\0\0\0\0"
        )
        diff_block = len(order) % 8
        zeros = diff_block and (b"\0" * (8 - diff_block)) or b""
        key = cipher.encrypt(str.encode(order + zeros.decode()))
        if isinstance(params64, str):
            params64 = params64.encode()
        dig = hmac.new(key=key, msg=params64, digestmod=hashlib.sha256).digest()
        return base64.b64encode(dig).decode()

    def redsys_form_generate_values(self, values):
        self.ensure_one()
        redsys_values = dict(values)
        merchant_parameters = self._prepare_merchant_parameters(values).decode('utf-8')
        redsys_values.update(
            {
                'api_url': self.redsys_get_form_action_url(),
                "Ds_SignatureVersion": str(self.redsys_signature_version),
                "Ds_MerchantParameters": merchant_parameters,
                "Ds_Signature": (self.sign_parameters(self.redsys_secret_key, merchant_parameters)),
            }
        )
        return redsys_values

    def redsys_get_form_action_url(self):
        self.ensure_one()
        environment = "prod" if self.state == "enabled" else "test"
        return self._get_redsys_urls(environment)["redsys_form_url"]

    def _product_description(self, order_ref):
        sale_order = self.env["sale.order"].search([("name", "=", order_ref)])
        res = ""
        if sale_order:
            description = "|".join(x.name for x in sale_order.order_line)
            res = description[:125]
        return res

    @api.model
    def redsys_s2s_form_process(self, data):
        if not data.get('token'):
            return False
        vals = {
            'name': 'Expired %s' % data.get('card'),
            'partner_id': data.get('partner_id'),
            'acquirer_id': data.get('acquirer_id'),
            'acquirer_ref': data.get('token'),
            'txnid': data.get('txnid')
        }
        return self.env['payment.token'].sudo().create(vals)

    def _prepare_merchant_parameters_recurring(self, tx_values):
        values = {
            "DS_MERCHANT_IDENTIFIER": tx_values.get('token_ref'),
            "DS_MERCHANT_COF_INI": "N",
            "DS_MERCHANT_COF_TXNID": tx_values.get('txnid'),
            "DS_MERCHANT_MERCHANTCODE": self.redsys_merchant_code and self.redsys_merchant_code[:9],
            "DS_MERCHANT_TRANSACTIONTYPE": (self.redsys_transaction_type or "0"),
            "DS_MERCHANT_EXCEP_SCA" : "MIT",
            "DS_MERCHANT_DIRECTPAYMENT": "true",
            "Ds_Merchant_Order": tx_values["reference"] and tx_values["reference"][-12:] or False,
            "DS_MERCHANT_TERMINAL": self.redsys_terminal or "1",
            "DS_MERCHANT_CURRENCY": self.redsys_currency or "978",
            "DS_MERCHANT_AMOUNT": str(int(round(tx_values["amount"] * 100))),
        }
        return self._url_encode64(json.dumps(values))


class PaymentToken(models.Model):
    _inherit = 'payment.token'

    txnid = fields.Char(string='Ds_Merchant_Cof_Txnid')

