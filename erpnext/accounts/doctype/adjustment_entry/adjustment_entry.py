# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _, ValidationError
from frappe.utils import flt, comma_or
from erpnext.controllers.accounts_controller import AccountsController
from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_outstanding_invoices, get_negative_outstanding_invoices, get_allow_cost_center_in_entry_of_bs_account
from erpnext.setup.utils import get_exchange_rate
from erpnext.accounts.doctype.payment_entry.payment_entry import get_company_defaults

class InvalidAdjustmentEntry(ValidationError):
	pass


class AdjustmentEntry(AccountsController):
    def validate(self):
        self.validate_customer_supplier_account()
        self.validate_reference_documents()
        self.clear_unallocated_reference_document_rows()

    def on_submit(self):
        if self.difference_amount:
            frappe.throw(_("Difference Amount must be zero"))
        self.make_gl_entries()

    def on_cancel(self):
        self.make_gl_entries(cancel=1)

    def validate_customer_supplier_account(self):
        customer_account_currency = self.customer_account_currency
        supplier_account_currency = self.supplier_account_currency
        customer_supplier_currency = self.customer_account_currency
        if self.customer and self.supplier and customer_account_currency != supplier_account_currency:
            frappe.throw(_("Customer account currency ({0}) and supplier account currency ({1}) should be same")
                         .format(customer_account_currency, supplier_account_currency), InvalidAdjustmentEntry)
        elif customer_supplier_currency and customer_supplier_currency != self.payment_currency and self.company_currency != customer_supplier_currency:
            frappe.throw(_("Payment currency ({0}) should be same as Customer/Supplier account currency ({1})")
                         .format(self.payment_currency, customer_supplier_currency), InvalidAdjustmentEntry)

    def validate_company_exchange_gain_loss_account(self):
        company_details = get_company_defaults(self.company)
        exchange_gain_loss_account = company_details.exchange_gain_loss_account
        if exchange_gain_loss_account is None:
            frappe.throw("Exchange gain loss account not set for {0}").format(self.company)

    def validate_reference_documents(self):
        valid_reference_doctypes = ("Sales Invoice", "Purchase Invoice", "Journal Entry")
        for d in self.debit_entries + self.credit_entries:
            if d.voucher_type not in valid_reference_doctypes:
                frappe.throw(_("Reference Doctype must be one of {0}")
                             .format(comma_or(valid_reference_doctypes)))
            if not frappe.db.exists(d.voucher_type, d.voucher_number):
                frappe.throw(_("{0} {1} does not exist").format(d.voucher_type, d.voucher_number))
            else:
                ref_doc = frappe.get_doc(d.voucher_type, d.voucher_number)
                if d.voucher_type in ("Sales Invoice", "Purchase Invoice"):
                    if d.voucher_type == "Sales Invoice" and ref_doc.debit_to != self.customer_account:
                        frappe.throw(_("{0} {1} is associated with {2}, but Party Account is {3}")
                                     .format(d.voucher_type, d.voucher_number, ref_doc.debit_to,
                                             self.customer_account))
                    elif d.voucher_type == "Purchase Invoice" and ref_doc.credit_to != self.supplier_account:
                        frappe.throw(_("{0} {1} is associated with {2}, but Party Account is {3}")
                                     .format(d.voucher_type, d.voucher_number, ref_doc.credit_to,
                                             self.supplier_account))
                if ref_doc.docstatus != 1:
                    frappe.throw(_("{0} {1} must be submitted")
                                 .format(d.voucher_type, d.voucher_number))
            if (flt(d.allocated_amount)) > 0:
                if flt(d.allocated_amount, d.precision("allocated_amount")) > flt(d.voucher_payment_amount, d.precision("voucher_payment_amount")):
                    frappe.throw(
                        _("{0} Row #{1}: Allocated Amount cannot be greater than outstanding amount.").format(d.parentfield, d.idx))

    # Clear the reference document which doesn't have allocated amount on validate so that form can be loaded fast
    def clear_unallocated_reference_document_rows(self):
        self.set("debit_entries", self.get("debit_entries", {"allocated_amount": ["not in", [0, None, ""]]}))
        self.set("credit_entries", self.get("credit_entries", {"allocated_amount": ["not in", [0, None, ""]]}))
        frappe.db.sql("""delete from `tabAdjustment Entry Reference`
     			where parent = %s and allocated_amount = 0""", self.name)

    def get_unreconciled_entries(self):
        self.allocate_payment_amount = False
        self.check_mandatory_to_fetch()
        self.get_entries()
        self.calculate_summary_totals()

    def check_mandatory_to_fetch(self):
        for fieldname in self.get_mandatory_fields():
            if not self.get(fieldname):
                frappe.throw(_("Please select {0} first").format(self.meta.get_label(fieldname)))

    def get_mandatory_fields(self):
        return ["company", "customer", "supplier"]

    def set_party_account_details(self, party_type='', party=''):
        account = get_party_account(party_type, party, self.company)
        account_currency = frappe.db.get_value("Account", account,
                                               'account_currency')
        self.set(party_type.lower() + "_account", account)
        self.set(party_type.lower() + "_account_currency", account_currency)

    def get_party_details(self, type="debit_entries"):
        if type == 'debit_entries':
            party_type = "Customer"
            party = self.customer
        else:
            party_type = "Supplier"
            party = self.supplier
        order_doctype = "Sales Order" if party_type == "Customer" else "Purchase Order"
        return [party_type, party, order_doctype]

    def add_invoice_currency_exchange_rate(self, currency):
        if any(exchange_rate.currency == currency for exchange_rate in self.get('exchange_rates')):
            return
        exc = self.append('exchange_rates', {})
        exc.currency = currency
        exc.exchange_rate_to_payment_currency = get_exchange_rate(currency, self.payment_currency) or 1
        exc.exchange_rate_to_base_currency = get_exchange_rate(currency, self.company_currency) or 1

    def get_exchange_rates(self, entries):
        currencies = list(set([entry.get("currency") for entry in entries]))
        if self.payment_currency not in currencies:
            currencies.append(self.payment_currency)
        self.set('exchange_rates', [])
        for currency in currencies:
            self.add_invoice_currency_exchange_rate(currency)

    def exchange_rates_to_dict(self):
        rates = {}
        for exchange_rate in self.exchange_rates:
            rates[exchange_rate.currency] = {
                "exchange_rate_to_payment_currency": exchange_rate.exchange_rate_to_payment_currency,
                "exchange_rate_to_base_currency": exchange_rate.exchange_rate_to_base_currency
            }
        return rates

    def get_entries(self):
        sales_invoices = self.get_positive_outstanding_entries('debit_entries')
        credit_notes = self.get_negative_outstanding_entries('debit_entries')
        purchase_invoices = self.get_positive_outstanding_entries('credit_entries')
        debit_notes = self.get_negative_outstanding_entries('credit_entries')
        self.get_exchange_rates(sales_invoices+purchase_invoices+credit_notes+debit_notes)
        self.add_invoice_entries(sales_invoices+debit_notes, 'debit_entries')
        self.add_invoice_entries(purchase_invoices+credit_notes, 'credit_entries')

    def get_positive_outstanding_entries(self, field_name):
        party_type, party, order_doctype = self.get_party_details(field_name)
        party_account = self.get(party_type.lower() + "_account")
        condition = ""
        # Add cost center condition
        if self.cost_center and get_allow_cost_center_in_entry_of_bs_account():
            condition += " and cost_center='%s'" % self.cost_center
        positive_outstanding_invoices = get_outstanding_invoices(party_type, party, party_account, condition=condition, filters={"outstanding_amt_greater_than": 0})
        self.get_extra_invoice_details(positive_outstanding_invoices)
        return positive_outstanding_invoices

    def get_negative_outstanding_entries(self, field_name):
        party_type, party, order_doctype = self.get_party_details(field_name)
        party_account = self.get(party_type.lower() + "_account")
        party_account_currency = self.get(party_type.lower() + "_account_currency")
        cost_center = self.cost_center if get_allow_cost_center_in_entry_of_bs_account() else None
        negative_outstanding_invoices = get_negative_outstanding_invoices(party_type,
                                                                          party, party_account,
                                                                          self.company,
                                                                          party_account_currency,
                                                                          self.company_currency, cost_center)
        self.get_extra_invoice_details(negative_outstanding_invoices)
        return negative_outstanding_invoices

    def get_extra_invoice_details(self, outstanding_invoices):
        for d in outstanding_invoices:
            d["exchange_rate"] = 1
            if d.voucher_type in ("Sales Invoice", "Purchase Invoice"):
                d["exchange_rate"], d["currency"], d["cost_center"] = frappe.db.get_value(d.voucher_type, d.voucher_no, ["conversion_rate", "currency", "cost_center"])
            if d.voucher_type in ("Journal Entry"):
                debit_in_account_currency, debit, d["currency"], d["cost_center"] = frappe.db.get_value('GL Entry', d.name, ["debit_in_account_currency", "debit", "account_currency", "cost_center"])
                d["exchange_rate"] = debit / debit_in_account_currency
            if d.voucher_type in ("Purchase Invoice"):
                d["supplier_bill_no"], d["supplier_bill_date"] = frappe.db.get_value(d.voucher_type, d.voucher_no, ["bill_no", "bill_date"])

    def add_invoice_entries(self, invoices, field_name):
        exchange_rates = self.exchange_rates_to_dict()
        party_type, party, order_doctype = self.get_party_details(field_name)
        party_account_currency = self.get(party_type.lower() + "_account_currency")
        self.set(field_name, [])
        for invoice in invoices:
            ent = self.append(field_name, { "voucher_type": invoice.get('voucher_type'), "voucher_number": invoice.get('voucher_no') })
            self.set_reference_entry_details(ent, invoice, party_account_currency, exchange_rates)

    def set_reference_entry_details(self, ent, invoice, party_account_currency, exchange_rates):
        ent.voucher_date = invoice.get('posting_date')
        ent.currency = invoice.get("currency")
        ent.exchange_rate = invoice.get('exchange_rate') or invoice.get('conversion_rate')
        ent.cost_center = invoice.get('cost_center')
        if party_account_currency != self.company_currency:
            ent.voucher_base_amount = abs(invoice.get('outstanding_amount') * ent.exchange_rate)
            ent.voucher_amount = abs(invoice.get('outstanding_amount'))
        else:
            ent.voucher_base_amount = abs(invoice.get('outstanding_amount'))
            ent.voucher_amount = abs(ent.voucher_base_amount / ent.exchange_rate)
        ent.recalculate_amounts(self.payment_currency, exchange_rates)
        ent.supplier_bill_no = invoice.get('supplier_bill_no')
        ent.supplier_bill_date = invoice.get('supplier_bill_date')

    def recalculate_tables(self):
        debit_entries = self.debit_entries if hasattr(self, 'debit_entries') else []
        credit_entries = self.credit_entries if hasattr(self, 'credit_entries') else []
        self.get_exchange_rates(debit_entries + credit_entries)
        self.recalculate_references(['debit_entries', 'credit_entries'])

    def recalculate_references(self, reference_types):
        exchange_rates = self.exchange_rates_to_dict()
        for reference_type in reference_types:
            entries = self.get(reference_type)
            if entries:
                for ent in entries:
                    ent.recalculate_amounts(self.payment_currency, exchange_rates)
        self.calculate_summary_totals()

    def add_reference_doc_details(self, reference_type, voucher_type, voucher_number):
        ref_doc = frappe.get_doc(voucher_type, voucher_number)
        reference_entries = self.get(reference_type)
        self.add_invoice_currency_exchange_rate(ref_doc.get("currency"))
        exchange_rates = self.exchange_rates_to_dict()
        party_type, party, order_doctype = self.get_party_details("debit_entries" if voucher_type == 'Sales Invoice' else "credit_entries")
        party_account_currency = self.get(party_type.lower() + "_account_currency")
        if len([ent for ent in reference_entries if ent.voucher_number == voucher_number and ent.voucher_type == voucher_type]) > 1:
            frappe.throw(_("{0} {1} is already present in {2}").format(voucher_type, voucher_number, reference_type))
        ent = next((ent for ent in reference_entries if ent.voucher_number == voucher_number and ent.voucher_type == voucher_type), None)
        if ent:
            self.set_reference_entry_details(ent, ref_doc, party_account_currency, exchange_rates)
            self.calculate_summary_totals()

    def calculate_summary_totals(self):
        self.receivable_adjusted = flt(sum([flt(d.allocated_amount) for d in self.get("debit_entries")]), self.precision("receivable_adjusted"))
        self.payable_adjusted = flt(sum([flt(d.allocated_amount) for d in self.get("credit_entries")]), self.precision("payable_adjusted"))
        self.total_balance = abs(sum([flt(d.balance) for d in self.get("debit_entries")]) - sum([flt(d.balance) for d in self.get("credit_entries")]))
        self.total_gain_loss = sum([flt(d.gain_loss_amount) for d in self.get("debit_entries")]) + sum([flt(d.gain_loss_amount) for d in self.get("credit_entries")])
        self.difference_amount = flt(abs(self.receivable_adjusted - self.payable_adjusted), self.precision("difference_amount"))

    def allocate_amount_to_references(self):
        total_debit_outstanding = sum([flt(d.voucher_payment_amount) for d in self.get("debit_entries")])
        total_credit_outstanding = sum([flt(c.voucher_payment_amount) for c in self.get("credit_entries")])
        exchange_rates = self.exchange_rates_to_dict()
        allocate_order = ['credit_entries', 'debit_entries'] if total_debit_outstanding > total_credit_outstanding else ['debit_entries', 'credit_entries']
        for reference_type in allocate_order:
            allocated_oustanding = min(total_debit_outstanding, total_credit_outstanding)
            entries = self.get(reference_type)
            for ent in entries:
                ent.allocated_amount = 0
                if self.allocate_payment_amount:
                    if allocated_oustanding > 0:
                        if ent.voucher_payment_amount >= allocated_oustanding:
                            ent.allocated_amount = allocated_oustanding
                        else:
                            ent.allocated_amount = ent.voucher_payment_amount
                        allocated_oustanding -= flt(ent.allocated_amount)
                ent.recalculate_amounts(self.payment_currency, exchange_rates)
        self.calculate_summary_totals()

    def make_gl_entries(self, cancel=0, adv_adj=0):
        gl_entries = []
        self.add_party_gl_entries(gl_entries)
        self.add_gain_loss_entries(gl_entries)
        make_gl_entries(gl_entries, cancel=cancel, adv_adj=adv_adj)

    def add_party_gl_entries(self, gl_entries):
        party_details_dict = dict()

        for reference_type in ['debit_entries', 'credit_entries']:
            party_type, party, order_doctype = self.get_party_details(reference_type)
            party_account = self.get(party_type.lower() + "_account")
            party_account_currency = self.get(party_type.lower() + "_account_currency")
            party_details_dict[reference_type] = dict({'party_type': party_type, 'party': party, 'account': party_account, 'order_doctype': order_doctype, 'account_currency': party_account_currency})

        for reference_type in ['debit_entries', 'credit_entries']:
            entries = self.get(reference_type)
            for ent in entries:
                ledger_reference_type = reference_type
                if ent.voucher_type == 'Purchase Invoice':
                    ledger_reference_type = 'credit_entries'
                if ent.voucher_type == 'Sales Invoice':
                    ledger_reference_type = 'debit_entries'
                party_details = party_details_dict[ledger_reference_type]
                against_account = party_details_dict['credit_entries'][
                    'account'] if ledger_reference_type == 'debit_entries' else party_details_dict['debit_entries']['account']
                dr_or_cr = "credit" if reference_type == 'debit_entries' else "debit"
                party_gl_dict = self.get_gl_dict({
                    "account": party_details['account'],
                    "party_type": party_details['party_type'],
                    "party": party_details['party'],
                    "against": against_account,
                    "account_currency": party_details['account_currency'],
                    "cost_center": ent.cost_center or self.cost_center
                 })
                party_gl_dict.update({
                    "against_voucher_type": ent.voucher_type,
                    "against_voucher": ent.voucher_number
                })
                allocated_amount_in_entry_currrency = ent.allocated_amount / ent.payment_exchange_rate
                allocated_amount_in_company_currency = allocated_amount_in_entry_currrency * ent.exchange_rate
                allocated_amount_in_account_currrency = ent.allocated_amount * ent.payment_exchange_rate if party_details['account_currency'] != self.company_currency else allocated_amount_in_company_currency
                party_gl_dict.update({
                    dr_or_cr + "_in_account_currency": allocated_amount_in_account_currrency,
                    dr_or_cr: allocated_amount_in_company_currency
                })
                gl_entries.append(party_gl_dict)

    def add_gain_loss_entries(self, gl_entries):
        company_details = get_company_defaults(self.company)
        exchange_gain_loss_account = company_details.exchange_gain_loss_account
        if exchange_gain_loss_account is None:
            frappe.throw("Exchange gain loss account not set for {0}").format(self.company)
        account_root_type = frappe.db.get_value("Account", exchange_gain_loss_account, "root_type")
        gl_dict = self.get_gl_dict({
                    "account": exchange_gain_loss_account,
                    "account_currency": self.company_currency,
                    "cost_center": self.cost_center or company_details.cost_center,
                 })
        if account_root_type == "Expense":
            dr_or_cr = "credit" if self.total_gain_loss > 0 else "debit"
        else:
            dr_or_cr = "debit" if self.total_gain_loss > 0 else "credit"
        gl_dict.update({
            dr_or_cr + "_in_account_currency": abs(self.total_gain_loss),
            dr_or_cr: abs(self.total_gain_loss)
        })
        gl_entries.append(gl_dict)