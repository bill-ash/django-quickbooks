from itertools import starmap
from uuid import uuid4, uuid1

from django.contrib.auth.hashers import make_password, check_password
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import JSONField
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.utils import timezone
from lxml import etree

from django_quickbooks import qbwc_settings, QUICKBOOKS_ENUMS
from django_quickbooks.exceptions import QBOperationNotFound
from django_quickbooks.managers import RealmQuerySet, RealmSessionQuerySet, QBDTaskQuerySet
from django_quickbooks.objects import import_object_cls, ItemService as QBDItemService, InvoiceLine as QBDInvoiceLine, \
    Account as QBDAccount
from django_quickbooks.objects.account import SalesOrPurchase  as QBDSalesOrPurchase
from django_quickbooks.objects.address import BillAddress as QBDBillAddress
from django_quickbooks.objects.customer import Customer as QBDCustomer
from django_quickbooks.objects.invoice import Invoice as QBDInvoice


# Below models are concrete implementations of above classes
# As initially I was working with django-tenant-schemas package I had to convert to that architecture
# Thus, below models are extended with schema_name concept that is the core of the django-tenant-schemas package:
# For more information: https://github.com/bernardopires/django-tenant-schemas

# NOTICE: you also find decorators in the package that only work with django-tenant-schemas


class Realm(models.Model):
    id = models.UUIDField(primary_key=True, editable=False, default=uuid4)
    schema_name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    name = models.CharField(max_length=155)
    password = models.CharField(max_length=128, null=True)

    objects = RealmQuerySet.as_manager()

    def set_password(self, raw_password):
        self.password = make_password(raw_password)
        self._password = raw_password

    def check_password(self, raw_password):
        """
        Return a boolean of whether the raw_password was correct. Handles
        hashing formats behind the scenes.
        """

        def setter(raw_password):
            self.set_password(raw_password)
            # Password hash upgrades shouldn't be considered password changes.
            self._password = None
            self.save(update_fields=["password"])

        return check_password(raw_password, self.password, setter)


class RealmSession(models.Model):
    id = models.UUIDField(verbose_name='Ticket for QBWC session', primary_key=True, editable=False, default=uuid1)
    realm = models.ForeignKey(Realm, on_delete=models.CASCADE, related_name='sessions')
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True)

    objects = RealmSessionQuerySet.as_manager()

    def close(self):
        self.ended_at = timezone.now()
        self.save()


class QBDTask(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    realm = models.ForeignKey(Realm, on_delete=models.CASCADE, related_name='qb_tasks')
    qb_operation = models.CharField(max_length=25)
    qb_resource = models.CharField(max_length=50)
    object_id = models.CharField(max_length=50, null=True)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True)
    content_object = GenericForeignKey()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = QBDTaskQuerySet.as_manager()

    def get_request(self):
        obj_class = import_object_cls(self.qb_resource)
        service = obj_class.get_service()()
        obj = self.content_object if self.content_type and self.object_id else None

        if self.qb_operation == QUICKBOOKS_ENUMS.OPP_QR:
            return service.all()
        elif not obj:
            raise ObjectDoesNotExist
        elif self.qb_operation == QUICKBOOKS_ENUMS.OPP_MOD:
            return service.update(obj.to_qbd_obj())
        elif self.qb_operation == QUICKBOOKS_ENUMS.OPP_ADD:
            return service.add(obj.to_qbd_obj())
        elif self.qb_operation == QUICKBOOKS_ENUMS.OPP_DEL:
            return service.delete(obj.to_qbd_obj())
        elif self.qb_operation == QUICKBOOKS_ENUMS.OPP_VOID:
            return service.void(obj.to_qbd_obj())
        else:
            raise QBOperationNotFound


class Customer(models.Model):
    id = models.UUIDField(primary_key=True, blank=True, editable=False, default=uuid4)
    realm = models.ForeignKey(Realm, on_delete=models.CASCADE, related_name='customers')
    list_id = models.CharField(max_length=127, unique=True, null=True, editable=False)
    edit_sequence = models.CharField(max_length=127, null=True, editable=False)
    full_name = models.CharField(max_length=100, null=True)
    name = models.CharField(max_length=41, null=True)
    is_active = models.BooleanField(default=True)
    time_created = models.DateTimeField(null=True)
    time_modified = models.DateTimeField(null=True)
    company_name = models.CharField(max_length=41, null=True)
    phone = models.CharField(max_length=21, null=True)
    alt_phone = models.CharField(max_length=21, null=True)
    fax = models.CharField(max_length=21, null=True)
    email = models.EmailField(null=True)
    contact = models.CharField(max_length=41, null=True)
    alt_contact = models.CharField(max_length=41, null=True)
    external_id = models.CharField(max_length=36, null=True)
    external_updated_at = models.DateTimeField(null=True)

    class Meta:
        unique_together = ('name', 'realm')

    @property
    def is_qbd_obj_created(self):
        return self.list_id and self.edit_sequence

    def to_qbd_obj(self, **fields):
        if fields:
            data = dict(
                starmap(lambda key, value: (key, getattr(self, value) or ''), fields.items())
            )
        else:
            data = dict(
                Name=self.name,
                IsActive=self.is_active,
                CompanyName=self.company_name or '',
                Phone=self.phone or '',
                AltPhone=self.alt_phone or '',
                Fax=self.fax or '',
                Email=self.email or '',
                Contact=self.contact or '',
                AltContact=self.alt_contact or '',
            )
            if self.is_qbd_obj_created:
                data.update(
                    **dict(
                        ListID=self.list_id or '',
                        EditSequence=self.edit_sequence
                    )
                )
        return QBDCustomer(**data)


class Invoice(models.Model):
    id = models.UUIDField(primary_key=True, blank=True, editable=False, default=uuid4)
    realm = models.ForeignKey(Realm, on_delete=models.CASCADE, related_name='invoices')
    list_id = models.CharField(max_length=127, unique=True, null=True, editable=False)
    edit_sequence = models.CharField(max_length=127, null=True, editable=False)
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name='invoice')
    time_created = models.DateTimeField(null=True)
    time_modified = models.DateTimeField(null=True)
    txn_date = models.DateField(null=True)
    is_pending = models.BooleanField(null=True)
    due_date = models.DateField(null=True)
    memo = models.TextField(null=True, max_length=4095)
    external_id = models.CharField(max_length=36, null=True)
    external_updated_at = models.DateTimeField(null=True)

    @property
    def is_qbd_obj_created(self):
        return self.list_id and self.edit_sequence

    def to_qbd_obj(self, create=True, **fields):
        def get_bill_address(invoice):
            bill_address = dict(Addr1='', City='', State='', PostalCode='', Country='')

            if hasattr(invoice, 'billaddress'):
                bill_address.update(
                    Addr1=invoice.billaddress.addresses.get('Addr1', ''),
                    City=invoice.billaddress.city or '',
                    State=invoice.billaddress.state or '',
                    PostalCode=str(invoice.billaddress.postal_code) or '',
                    Country=invoice.billaddress.country or '',
                )

            return QBDBillAddress(**bill_address)

        def get_invoice_lines(invoice):
            invoice_lines = []
            for invoice_line in invoice.invoice_lines.all():
                item_group = QBDItemService(ListID=invoice_line.type.list_id if invoice_line.type.list_id else '')
                invoice_lines.append(QBDInvoiceLine(Item=item_group, Quantity=1.0, Rate=float(invoice_line.rate)))
            return invoice_lines

        data = dict(
            Customer=self.customer.to_qbd_obj(**dict(ListID='list_id')),
            BillAddress=get_bill_address(self),
            IsPending=self.is_pending,
            DueDate=self.due_date.isoformat(),
            InvoiceLine=get_invoice_lines(self),
        )
        if self.is_qbd_obj_created:
            data.update(
                **dict(
                    TxnID=self.list_id,
                    EditSequence=self.edit_sequence
                )
            )

        return QBDInvoice(**data)


class ItemService(models.Model):
    id = models.UUIDField(primary_key=True, blank=True, editable=False, default=uuid4)
    realm = models.ForeignKey(Realm, on_delete=models.CASCADE, related_name='item_services')
    list_id = models.CharField(max_length=127, unique=True, null=True, editable=False)
    edit_sequence = models.CharField(max_length=127, null=True, editable=False)
    name = models.CharField(max_length=25, null=True, blank=True)
    account = models.ForeignKey('ServiceAccount', on_delete=models.CASCADE, related_name='item_services')
    description = models.TextField(null=True, blank=True)

    class Meta:
        unique_together = ('name', 'realm')

    @property
    def is_qbd_obj_created(self):
        return self.list_id and self.edit_sequence

    def to_qbd_obj(self, **fields):
        data = dict(
            Name=self.name,
            IsActive=True,
            SalesOrPurchase=QBDSalesOrPurchase(Account=QBDAccount(ListID=self.account.list_id))
        )
        if self.is_qbd_obj_created:
            data.update(
                **dict(
                    ListID=self.list_id,
                    EditSequence=self.edit_sequence
                )
            )

        return QBDItemService(**data)

    @classmethod
    def from_qbd_obj(cls, qbd_obj, realm_id=None):
        return cls(
            realm_id=realm_id,
            name=qbd_obj.Name,
            list_id=qbd_obj.ListID,
            esit_sequence=qbd_obj.EditSequence
        )


class ServiceAccount(models.Model):
    id = models.UUIDField(primary_key=True, blank=True, editable=False, default=uuid4)
    realm = models.ForeignKey(Realm, on_delete=models.CASCADE, related_name='service_accounts')
    list_id = models.CharField(max_length=127, unique=True, null=True, editable=False)
    edit_sequence = models.CharField(max_length=127, null=True, editable=False)
    name = models.CharField(max_length=31)
    full_name = models.CharField(max_length=159, null=True)
    is_active = models.BooleanField(default=True)
    parent_id = models.CharField(max_length=127, null=True, editable=False)
    account_type = models.CharField(max_length=100, null=True)
    account_number = models.IntegerField(null=True)

    @classmethod
    def from_qbd_obj(cls, qbd_obj, realm_id=None):
        return cls(
            realm_id=realm_id,
            name=qbd_obj.Name,
            full_name=qbd_obj.FullName,
            is_active=qbd_obj.IsActive,
            parent_id=qbd_obj.ParentRef.ListID if hasattr(qbd_obj, 'ParentRef') else None,
            account_type=qbd_obj.AccountType,
            account_number=qbd_obj.AccountNumber,
            list_id=qbd_obj.ListID,
            edit_sequence=qbd_obj.EditSequence,
        )


class ExternalItemService(models.Model):
    charge_type = models.ForeignKey(ItemService, on_delete=models.CASCADE, related_name='external_item_service')
    external_item_service_id = models.CharField(max_length=36, null=False, blank=False)

    class Meta:
        unique_together = ('charge_type', 'external_item_service_id')


class InvoiceLine(models.Model):
    id = models.UUIDField(primary_key=True, blank=True, editable=False, default=uuid4)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='invoice_lines')
    realm = models.ForeignKey(Realm, on_delete=models.CASCADE, related_name='invoice_lines')
    type = models.ForeignKey(ItemService, on_delete=models.SET_NULL, null=True, related_name='invoice_charges')
    rate = models.DecimalField(decimal_places=2, max_digits=8)
    note = models.TextField(max_length=150, null=True, blank=True)
    external_id = models.CharField(max_length=36, null=True)


class AbstractAddress(models.Model):
    invoice = models.OneToOneField(Invoice, on_delete=models.CASCADE, related_name='%(class)s')
    addresses = JSONField(default=dict)
    city = models.CharField(max_length=31, null=True)
    state = models.CharField(max_length=21, null=True)
    postal_code = models.CharField(max_length=13, null=True)
    country = models.CharField(max_length=31, null=True)
    note = models.CharField(max_length=41, null=True)

    class Meta:
        abstract = True


class BillAddress(AbstractAddress):
    pass


class ShipAddress(AbstractAddress):
    pass


def create_qwc(realm, **kwargs):
    root = etree.Element("QBWCXML")

    app_name = kwargs.pop('app_name', qbwc_settings.APP_NAME)
    msg = etree.SubElement(root, 'AppName')
    msg.text = str(app_name)

    app_id = kwargs.pop('app_id', qbwc_settings.APP_ID)
    msg = etree.SubElement(root, 'AppID')
    msg.text = str(app_id)

    app_url = kwargs.pop('app_url', qbwc_settings.APP_URL)
    msg = etree.SubElement(root, 'AppURL')
    msg.text = str(app_url)

    app_desc = kwargs.pop('app_desc', qbwc_settings.APP_DESCRIPTION)
    msg = etree.SubElement(root, 'AppDescription')
    msg.text = str(app_desc)

    app_support = kwargs.pop('app_support', qbwc_settings.APP_SUPPORT)
    msg = etree.SubElement(root, 'AppSupport')
    msg.text = str(app_support)

    username = realm.id
    msg = etree.SubElement(root, 'UserName')
    msg.text = str(username)

    owner_id = kwargs.pop('owner_id', qbwc_settings.OWNER_ID)
    msg = etree.SubElement(root, 'OwnerID')
    msg.text = str(owner_id)

    file_id = kwargs.pop('file_id', '{%s}' % uuid1())
    msg = etree.SubElement(root, 'FileID')
    msg.text = str(file_id)

    qb_type = kwargs.pop('qb_type', qbwc_settings.QB_TYPE)
    msg = etree.SubElement(root, 'QBType')
    msg.text = str(qb_type)

    msg = etree.SubElement(root, 'Scheduler')

    schedule_n_minutes = kwargs.pop('schedule_n_minutes', qbwc_settings.MINIMUM_RUN_EVERY_NMINUTES)
    n_minutes = etree.SubElement(msg, 'RunEveryNMinutes')
    n_minutes.text = str(schedule_n_minutes)

    tree = etree.ElementTree(root)

    return etree.tostring(tree, xml_declaration=True, encoding='UTF-8')
