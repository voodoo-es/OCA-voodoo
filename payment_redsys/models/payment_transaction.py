import base64
import json
import urllib
import requests
import logging
from odoo.tools import config
from odoo.exceptions import UserError
from odoo import _, api, fields, http, models
from odoo.tools.float_utils import float_compare
from odoo.addons.payment.models.payment_acquirer import ValidationError

_logger = logging.getLogger(__name__)


class TxRedsys(models.Model):
    _inherit = "payment.transaction"

    redsys_txnid = fields.Char("Transaction ID")

    def _get_specific_processing_values(self, processing_values):
        """ Return a dict of acquirer-specific values used to process the transaction.

        For an acquirer to add its own processing values, it must overwrite this method and return a
        dict of acquirer-specific values based on the generic values returned by this method.
        Acquirer-specific values take precedence over those of the dict of generic processing
        values.

        :param dict processing_values: The generic processing values of the transaction
        :return: The dict of acquirer-specific processing values
        :rtype: dict
        """
        res = super()._get_specific_processing_values(processing_values)
        if self.provider != 'redsys':
            return res
        self.redsys_s2s_do_transaction()
        return dict()

    def _get_specific_rendering_values(self, processing_values):
        """ Return a dict of acquirer-specific values used to render the redirect form.

        For an acquirer to add its own rendering values, it must overwrite this method and return a
        dict of acquirer-specific values based on the processing values (acquirer-specific
        processing values included).

        :param dict processing_values: The processing values of the transaction
        :return: The dict of acquirer-specific rendering values
        :rtype: dict
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider != 'redsys':
            return res

        return self.acquirer_id.redsys_form_generate_values(processing_values)

    def _send_payment_request(self):
        """ Override of payment to send a payment request to Redsys.

        Note: self.ensure_one()

        :return: None
        :raise: UserError if the transaction is not linked to a token
        """
        super()._send_payment_request()
        if self.provider != 'redsys':
            return

        if not self.token_id:
            raise UserError(_("Redsys: " + _("The transaction is not linked to a token.")))

        tx_values = {
            'token_ref': self.token_id.acquirer_ref,
            'txnid': self.token_id.txnid,
            'reference': self.reference,
            'amount': self.amount
        }

        merchant_parameters = self.acquirer_id._prepare_merchant_parameters_recurring(tx_values)
        redsys_values = {
            "Ds_SignatureVersion": str(self.acquirer_id.redsys_signature_version),
            "Ds_MerchantParameters": merchant_parameters,
            "Ds_Signature": self.acquirer_id.sign_parameters(
                self.acquirer_id.redsys_secret_key, merchant_parameters
            ),
        }

        url = self.acquirer_id._get_redsys_url_s2s()
        response = requests.post(url=url, data=redsys_values)
        response = json.loads(response.content.decode('utf8'))
        if response.get('errorCode', False):
            _logger.debug("======= ERROR FROM REDSYS: =====%r", response.get('errorCode', False))
            return

        self._handle_feedback_data('redsys', response)

    @staticmethod
    def merchant_params_json2dict(data):
        parameters = data.get("Ds_MerchantParameters", "")
        return json.loads(base64.b64decode(parameters).decode())

    # --------------------------------------------------
    # FORM RELATED METHODS
    # --------------------------------------------------

    @api.model
    def _redsys_form_get_tx_from_data(self, data):
        """ Given a data dict coming from redsys, verify it and
        find the related transaction record. """
        parameters = data.get("Ds_MerchantParameters", "")
        parameters_dic = json.loads(base64.b64decode(parameters).decode())
        reference = urllib.parse.unquote(parameters_dic.get("Ds_Order", ""))
        pay_id = parameters_dic.get("Ds_AuthorisationCode")
        shasign = data.get("Ds_Signature", "").replace("_", "/").replace("-", "+")
        test_env = config["test_enable"]
        if not reference or not pay_id or not shasign:
            error_msg = (
                    "Redsys: received data with missing reference"
                    " (%s) or pay_id (%s) or shashign (%s)" % (reference, pay_id, shasign)
            )
            if not test_env:
                _logger.error(error_msg)
                raise ValidationError(error_msg)
            # For tests
            http.OpenERPSession.tx_error = True
        tx = self.search([("reference", "=", reference)])
        if not tx or len(tx) > 1:
            error_msg = "Redsys: received data for reference %s" % (reference)
            if not tx:
                error_msg += "; no order found"
            else:
                error_msg += "; multiple order found"
            if not test_env:
                _logger.error(error_msg)
                raise ValidationError(error_msg)
            # For tests
            http.OpenERPSession.tx_error = True
        if tx and not test_env:
            # verify shasign
            shasign_check = tx.acquirer_id.sign_parameters(
                tx.acquirer_id.redsys_secret_key, parameters
            )
            if shasign_check != shasign:
                error_msg = (
                        "Redsys: invalid shasign, received %s, computed %s, "
                        "for data %s" % (shasign, shasign_check, data)
                )
                _logger.error(error_msg)
                raise ValidationError(error_msg)
        return tx

    def _redsys_form_get_invalid_parameters(self, data):
        test_env = config["test_enable"]
        invalid_parameters = []
        parameters_dic = self.merchant_params_json2dict(data)
        if (self.acquirer_reference and parameters_dic.get("Ds_Order")) != self.acquirer_reference:
            invalid_parameters.append(
                (
                    "Transaction Id",
                    parameters_dic.get("Ds_Order"),
                    self.acquirer_reference,
                )
            )

        # check what has been bought
        if self.acquirer_id.redsys_percent_partial > 0.0:
            new_amount = self.amount - (self.amount * self.acquirer_id.redsys_percent_partial / 100)
            self.amount = new_amount

        if float_compare(float(parameters_dic.get("Ds_Amount", "0.0")) / 100, self.amount, 2) != 0:
            invalid_parameters.append(("Amount", parameters_dic.get("Ds_Amount"), "%.2f" % self.amount))

        if invalid_parameters and test_env:
            # If transaction is in test mode invalidate invalid_parameters
            # to avoid logger error from parent method
            return []
        return invalid_parameters

    @api.model
    def _get_redsys_state(self, status_code):
        if 0 <= status_code <= 100:
            return "done"
        elif status_code <= 203:
            return "pending"
        elif 912 <= status_code <= 9912:
            return "cancel"
        else:
            return "error"

    def _process_feedback_data(self, data):
        super()._process_feedback_data(data)
        if self.provider != 'redsys':
            return
        params = self.merchant_params_json2dict(data)
        status_code = int(params.get("Ds_Response", "29999"))
        state = self._get_redsys_state(status_code)
        vals = {
            "state": state,
            # "redsys_txnid": params.get("Ds_AuthorisationCode"),
            # "date": fields.Datetime.now(),
        }
        state_message = ""
        if state == "done":
            vals["state_message"] = _("Ok: %s") % params.get("Ds_Response")
            order = self.env['sale.order'].search([('name', '=', params.get("Ds_Order").split('-')[0])])
            # acquirer = self.env['payment.acquirer'].search([('provider', '=', 'redsys')])
            # s2s_data = {
            #     'token': params.get('Ds_Merchant_Identifier'),
            #     'card': params.get('Ds_ExpiryDate'),
            #     'acquirer_id': acquirer.id,
            #     'partner_id': order.partner_id.id,
            #     'txnid': params.get('Ds_Merchant_Cof_Txnid')
            # }
            # if not self.token_id:
            #     token_id = self.acquirer_id.redsys_s2s_form_process(s2s_data)
            #     if token_id:
            #         self.token_id = token_id.id
            self._set_done()
            # self._finalize_post_processing()
            # self._execute_callback()
            self.env.cr.commit()
        elif state == "pending":  # 'Payment error: code: %s.'
            state_message = _("Error: %s (%s)")
            self._set_pending()
        elif state == "cancel":  # 'Payment error: bank unavailable.'
            state_message = _("Bank Error: %s (%s)")
            self._set_canceled()
        else:
            state_message = _("Redsys: feedback error %s (%s)")
            self._set_error(state_message)
        if state_message:
            vals["state_message"] = state_message % (
                params.get("Ds_Response"),
                params.get("Ds_ErrorCode"),
            )
            if state == "error":
                _logger.warning(vals["state_message"])
        self.write(vals)
        return state != "error"

    @api.model
    def _get_tx_from_feedback_data(self, acquirer_name, data):
        res = super()._get_tx_from_feedback_data(acquirer_name, data)
        if acquirer_name != "redsys":
            return res
        try:
            tx_find_method_name = "_%s_form_get_tx_from_data" % acquirer_name
            if hasattr(self, tx_find_method_name):
                tx = getattr(self, tx_find_method_name)(data)
            _logger.info(
                "<%s> transaction processed: tx ref:%s, tx amount: %s",
                acquirer_name,
                tx.reference if tx else "n/a",
                tx.amount if tx else "n/a",
            )
            if tx.acquirer_id.redsys_percent_partial > 0:
                if tx and tx.sale_order_ids and tx.sale_order_ids.ensure_one():
                    percent_reduction = tx.acquirer_id.redsys_percent_partial
                    new_so_amount = tx.sale_order_ids.amount_total - (
                            tx.sale_order_ids.amount_total * percent_reduction / 100
                    )
                    amount_matches = (
                            tx.sale_order_ids.state in ["draft", "sent"]
                            and float_compare(tx.amount, new_so_amount, 2) == 0
                    )
                    if amount_matches:
                        if tx.state == "done":
                            _logger.info(
                                "<%s> transaction completed, confirming order "
                                "%s (ID %s)",
                                acquirer_name,
                                tx.sale_order_ids.name,
                                tx.sale_order_ids.id,
                            )
                            if not self.env.context.get("bypass_test", False):
                                tx.sale_order_ids.with_context(
                                    send_email=True
                                ).action_confirm()
                        elif tx.state != "cancel" and tx.sale_order_ids.state == "draft":
                            _logger.info(
                                "<%s> transaction pending, sending "
                                "quote email for order %s (ID %s)",
                                acquirer_name,
                                tx.sale_order_ids.name,
                                tx.sale_order_ids.id,
                            )
                            if not self.env.context.get("bypass_test", False):
                                tx.sale_order_ids.action_quotation_send()
                    else:
                        _logger.warning(
                            "<%s> transaction MISMATCH for order " "%s (ID %s)",
                            acquirer_name,
                            tx.sale_order_ids.name,
                            tx.sale_order_ids.id,
                        )
        except Exception:
            _logger.exception(
                "Fail to confirm the order or send the confirmation email%s",
                tx and " for the transaction %s" % tx.reference or "",
            )
        return tx

    def redsys_s2s_do_transaction(self, **kwargs):
        tx_values = {
            'token_ref': self.token_id.acquirer_ref,
            'txnid': self.token_id.txnid,
            'reference': self.reference,
            'amount': self.amount
        }

        merchant_parameters = self.acquirer_id._prepare_merchant_parameters_recurring(tx_values)
        redsys_values = {
            "Ds_SignatureVersion": str(self.acquirer_id.redsys_signature_version),
            "Ds_MerchantParameters": merchant_parameters,
            "Ds_Signature": self.acquirer_id.sign_parameters(
                self.acquirer_id.redsys_secret_key, merchant_parameters
            ),
        }

        url = self.acquirer_id._get_redsys_url_s2s()
        response = requests.post(url=url, data=redsys_values)
        response = json.loads(response.content.decode('utf8'))
        if response.get('errorCode', False):
            _logger.debug("=======ERROR OF REDSYS=====%r", response.get('errorCode', False))
            return
        self._process_feedback_data(response)
        if self.state == 'done':
            self.renewal_allowed = True
            self._execute_callback()
