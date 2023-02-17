# Copyright 2017 Tecnativa - Sergio Teruel

{
    "name": "Pasarela de pago Redsys",
    "category": "Payment Acquirer",
    "summary": "Payment Acquirer: Redsys Implementation",
    "version": "15.0.1.0.4",
    "author": "Tecnativa," "Odoo Community Association (OCA)",
    "depends": ["payment", "website_sale"],
    "external_dependencies": {"python": ["pycrypto"]},
    "data": [
        "views/redsys.xml",
        "views/payment_acquirer.xml",
        "views/payment_redsys_templates.xml",
        "data/payment_redsys.xml",
    ],
    "license": "AGPL-3",
    "installable": True,
}
