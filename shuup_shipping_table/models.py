# -*- coding: utf-8 -*-
# This file is part of Shuup Shipping Table
#
# Copyright (c) 2016, Rockho Team. All rights reserved.
# Author: Christian Hess
#
# This source code is licensed under the AGPLv3 license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import unicode_literals

import logging
from datetime import timedelta
from decimal import Decimal

from enumfields import Enum, EnumIntegerField
from parler.fields import TranslatedField
from parler.models import TranslatedFields

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.encoding import python_2_unicode_compatible
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _
from django_countries.fields import CountryField

from shuup.core.fields import MeasurementField, MoneyValueField
from shuup.core.models._base import PolymorphicTranslatableShuupModel
from shuup.core.models._service_base import ServiceBehaviorComponent, ServiceCost
from shuup.core.models._shops import Shop
from shuup.utils.dates import DurationRange

logger = logging.getLogger(__name__)


class FetchTableMode(Enum):
    LOWEST_PRICE = 0
    LOWEST_DELIVERY_TIME = 1

    class Labels:
        LOWEST_PRICE = _('Lowest price')
        LOWEST_DELIVERY_TIME = _('Lowest delivery time')


class ShippingTableBehaviorComponent(ServiceBehaviorComponent):
    add_delivery_time_days = models.PositiveSmallIntegerField(verbose_name=_("additional delivery time"),
                                                              default=0,
                                                              help_text=_("Extra number of days to add."))

    add_price = MoneyValueField(verbose_name=_("additional price"),
                                default=Decimal(),
                                help_text=_("Extra amount to add."))

    class Meta:
        abstract = True

    def get_available_table_items(self, source):
        """
        Fetches the available table items
        """
        now_dt = now()
        weight = sum(((x.get("weight") or 0) for x in source.get_lines()), 0)

        # 1) source total weight must be in a range
        # 2) enabled tables
        # 3) enabled table carriers
        # 4) valid shops
        # 5) valid date range tables
        # 6) order by priority
        # 7) distinct rows

        qs = ShippingTableItem.objects.select_related('table').filter(
            end_weight__gte=weight,
            start_weight__lte=weight,
            table__enabled=True,
            table__carrier__enabled=True,
            table__shops__in=[source.shop]
        ).filter(
            Q(Q(table__start_date__lte=now_dt) | Q(table__start_date=None)),
            Q(Q(table__end_date__gte=now_dt) | Q(table__end_date=None))
        ).order_by('-region__priority').distinct()

        return qs

    def get_first_available_item(self, source):
        table_items = self.get_available_table_items(source)

        for table_item in table_items:
            # check if the table exclude region is compatible
            # with the source.. if True, check next one
            invalid_region = False
            for excluded_region in table_item.table.excluded_regions.all():
                if excluded_region.is_compatible_with(source):
                    invalid_region = True
                    break
            if invalid_region:
                continue

            # a valid table item was found! get out of here
            if table_item.region.is_compatible_with(source):
                return table_item

    def get_unavailability_reasons(self, service, source):
        table_item = self.get_first_available_item(source)

        if not table_item:
            return [ValidationError(_("No table found"))]
        return ()

    def get_costs(self, service, source):
        table_item = self.get_first_available_item(source)

        if table_item:
            return [ServiceCost(source.create_price(table_item.price + self.add_price))]

        return ()

    def get_delivery_time(self, service, source):
        table_item = self.get_first_available_item(source)

        if table_item:
            return DurationRange(
                timedelta(days=(table_item.delivery_time + self.add_delivery_time_days))
            )

        return None


class ShippingTableByModeBehaviorComponent(ShippingTableBehaviorComponent):
    name = _("Shipping Table: custom fetch mode")
    help_text = _("Fetches the price and delivery time using a custom mode.")

    mode = EnumIntegerField(FetchTableMode,
                            verbose_name=_('mode'),
                            help_text=_("Select the mode which will be used "
                                        "to fetch a shipping table."))

    tables = models.ManyToManyField('ShippingTable',
                                    verbose_name=_("tables filter"),
                                    blank=True,
                                    help_text=_("Only selected tables will be used "
                                                "to calculate shipping. "
                                                "Blank means all available tables."))

    carriers = models.ManyToManyField('ShippingCarrier',
                                      verbose_name=_("carriers filter"),
                                      blank=True,
                                      help_text=_("Only selected carriers will be used "
                                                  "to calculate shipping. "
                                                  "Blank means all carriers."))

    def get_available_table_items(self, source):
        """ Add extra filtering """

        table_items = super(
            ShippingTableByModeBehaviorComponent, self
        ).get_available_table_items(source)

        if self.tables.exists():
            table_items = table_items.filter(
                table__in=self.tables.all()
            )

        if self.carriers.exists():
            table_items = table_items.filter(
                table__carrier__in=self.carriers.all()
            )

        if self.mode == FetchTableMode.LOWEST_PRICE:
            table_items = table_items.order_by('-region__priority', 'price')
        elif self.mode == FetchTableMode.LOWEST_DELIVERY_TIME:
            table_items = table_items.order_by('-region__priority', 'delivery_time')

        return table_items


class SpecificShippingTableBehaviorComponent(ShippingTableBehaviorComponent):
    name = _("Shipping Table: specific table")
    help_text = _("Fetches the price and delivery time from a specific shipping table")

    table = models.ForeignKey('ShippingTable',
                              verbose_name=_("table"),
                              help_text=_("Select the table to fetch the price and delivery time."))

    def get_available_table_items(self, source):
        """ Add extra filtering """

        qs = super(
            SpecificShippingTableBehaviorComponent, self
        ).get_available_table_items(source).filter(
            table=self.table
        ).order_by('-region__priority', 'price')

        return qs


@python_2_unicode_compatible
class ShippingCarrier(models.Model):
    name = models.CharField(verbose_name=_("name"), max_length=40)
    enabled = models.BooleanField(verbose_name=_("enabled"), default=True)
    shops = models.ManyToManyField(Shop, blank=True,
                                   related_name="shop_shipping_carriers",
                                   verbose_name=_("shops"),
                                   help_text=_("Select the shops which can use this carrier. "
                                               "Blank means no shop!"))

    class Meta:
        verbose_name = _("shipping carrier")
        verbose_name_plural = _("shipping carriers")

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class ShippingRegion(PolymorphicTranslatableShuupModel):
    name = TranslatedField(any_language=True)
    description = TranslatedField()
    priority = models.IntegerField(verbose_name=_("priority"),
                                   default=0,
                                   help_text=_("A higher number means this region is most "
                                               "important than other with a lower priority"))

    base_translations = TranslatedFields(
        name=models.CharField(max_length=100, verbose_name=_("name")),
        description=models.CharField(max_length=120, blank=True, verbose_name=_("description"))
    )

    class Meta:
        verbose_name = _("shipping region")
        verbose_name_plural = _("shipping regions")

    def __str__(self):
        return self.name

    def is_compatible_with(self, source):
        """
        Returns true if this region is compatible for given source.

        This method should especially check the source's delivery address
        and return if this region is compatible

        :type source: shuup.core.order_creator.OrderSource
        :rtype: bool
        :return: whether this region is available for a source
        """
        return False


@python_2_unicode_compatible
class PostalCodeRangeShippingRegion(ShippingRegion):
    country = CountryField(verbose_name=_("country"))
    start_postal_code = models.PositiveIntegerField(verbose_name=_("starting postal code"))
    end_postal_code = models.PositiveIntegerField(verbose_name=_("ending postal code"))

    class Meta:
        verbose_name = _("shipping region by postal code range")
        verbose_name_plural = _("shipping regions by postal code range")
        ordering = ('priority', 'start_postal_code')

    def is_compatible_with(self, source):
        if not source.shipping_address or not source.shipping_address.postal_code or \
                source.shipping_address.country != self.country:
            return False

        postal_code_int = int("".join([d for d in source.shipping_address.postal_code if d.isdigit()]))
        return self.start_postal_code <= postal_code_int and self.end_postal_code >= postal_code_int

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class CountryShippingRegion(ShippingRegion):
    country = CountryField(verbose_name=_("country"), unique=True)

    class Meta:
        verbose_name = _("shipping region by country")
        verbose_name_plural = _("shipping regions by country")

    def is_compatible_with(self, source):
        if not source.shipping_address or not source.shipping_address.country:
            return False
        return source.shipping_address.country == self.country

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class ShippingTable(models.Model):
    identifier = models.SlugField(unique=True,
                                  verbose_name=_("identifier"),
                                  help_text=_("A slug identifier, e.g my-table-id-3218321"))
    name = models.CharField(verbose_name=_("name"), max_length=40)
    enabled = models.BooleanField(verbose_name=_("enabled"), default=True)
    carrier = models.ForeignKey(ShippingCarrier, verbose_name=_("carrier"))
    excluded_regions = models.ManyToManyField(ShippingRegion, blank=True,
                                              verbose_name=_("excluded regions"),
                                              help_text=_("Selected regions will be excluded "
                                                          "from this table"))
    start_date = models.DateTimeField(verbose_name=_("start date"), null=True, blank=True)
    end_date = models.DateTimeField(verbose_name=_("end date"), null=True, blank=True)
    shops = models.ManyToManyField(Shop, blank=True,
                                   related_name="shop_shipping_tables",
                                   verbose_name=_("shops"),
                                   help_text=_("Select the shops which can use this table. "
                                               "Blank means no shop!"))

    class Meta:
        verbose_name = _("shipping table")
        verbose_name_plural = _("shipping tables")

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class ShippingTableItem(models.Model):
    table = models.ForeignKey(ShippingTable, verbose_name=_("table"))
    region = models.ForeignKey(ShippingRegion, verbose_name=_("region"))

    start_weight = MeasurementField(unit="g", verbose_name=_('start weight (g)'))
    end_weight = MeasurementField(unit="g", verbose_name=_('end weight (g)'))

    price = MoneyValueField(verbose_name=_("price"))
    delivery_time = models.PositiveSmallIntegerField(verbose_name=_("delivery time (days)"))

    class Meta:
        verbose_name = _("shipping price table")
        verbose_name_plural = _("shipping price tables")

    def __str__(self):
        return "ID {0} {1} {2} - {3}->{4}: {5}-{6}".format(self.id,
                                                           self.table,
                                                           self.region,
                                                           self.start_weight,
                                                           self.end_weight,
                                                           self.price,
                                                           self.delivery_time)
