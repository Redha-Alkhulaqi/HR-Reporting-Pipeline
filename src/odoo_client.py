import os
import xmlrpc.client
from dotenv import load_dotenv

load_dotenv()

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")


def get_odoo_models():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
    if not uid:
        raise ValueError("Odoo authentication failed")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def fetch_employee_schedules():
    uid, models = get_odoo_models()

    employees = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "hr.employee",
        "search_read",
        [[]],
        {
            "fields": [
                "id",
                "employee_id",
                "name",
                "resource_calendar_id",
            ]
        },
    )

    return employees