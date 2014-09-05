import calendar
from decimal import Decimal
import logging

from collections import namedtuple
from datetime import datetime, timedelta
from flask import current_app
from sqlalchemy.schema import CheckConstraint
from cryptokit import bits_to_difficulty

from .model_lib import base
from .filters import sig_round
from . import db, currencies


def make_upper_lower(trim=None, span=None, offset=None, clip=None, fmt="dt"):
    """ Generates upper and lower bounded datetime objects. """
    dt = datetime.utcnow()

    if span is None:
        span = timedelta(minutes=10)

    if trim is not None:
        trim_seconds = trim.total_seconds()
        stamp = calendar.timegm(dt.utctimetuple())
        stamp = (stamp // trim_seconds) * trim_seconds
        dt = datetime.utcfromtimestamp(stamp)

    if offset is None:
        offset = timedelta(minutes=0)

    if clip is None:
        clip = timedelta(minutes=0)

    upper = dt - offset - clip
    lower = dt - span - offset

    if fmt == "both":
        return lower, upper, calendar.timegm(lower.utctimetuple()), calendar.timegm(upper.utctimetuple())
    elif fmt == "stamp":
        calendar.timegm(lower.utctimetuple()), calendar.timegm(upper.utctimetuple())
    return lower, upper


class TradeRequest(base):
    """
    Used to provide info necessary to external applications for trading currencies

    Created rows will be checked + updated externally
    """
    id = db.Column(db.Integer, primary_key=True)
    # 3-8 letter code for the currency to be traded
    currency = db.Column(db.String, nullable=False)
    # Quantity of currency to be traded
    quantity = db.Column(db.Numeric, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    type = db.Column(db.Enum("sell", "buy", name="req_type"), nullable=False)

    # These values should only be updated by sctrader
    exchanged_quantity = db.Column(db.Numeric, default=None)
    # Fees from fulfilling this tr
    fees = db.Column(db.Numeric, default=None)
    _status = db.Column(db.SmallInteger, default=0)

    @property
    def payouts(self):
        if self.type == "sell":
            return self.sell_payouts
        return self.buy_payouts

    @property
    def status(self):
        if self._status == 0:
            return "Pending Exchange Deposit"
        elif self._status == 2:
            return "Selling on Exchange"
        elif self._status == 4:
            return "Pending Exchange Withdrawal"
        elif self._status == 6:
            return "Complete"
        return "Error"

    @classmethod
    def create(cls, currency, quantity):
        tr = cls(currency=currency,
                 quantity=quantity)

        # add and flush
        db.session.add(tr)
        return tr


class BlockPayout(base):
    # The share chain that contributed this portion of shares to the block
    chainid = db.Column(db.Integer, primary_key=True)
    blockhash = db.Column(db.String, db.ForeignKey('block.hash'), primary_key=True)
    block = db.relationship('Block', foreign_keys=[blockhash], backref='block_payouts')
    # Placeholder for the point at which the block was solved in this share chain.
    solve_slice = db.Column(db.Integer)
    # Shares on this chain
    shares = db.Column(db.Numeric, nullable=False)
    # total going to pool from donations + fees
    contributed = db.Column(db.Numeric)
    # Total paid out in bonuses
    bonus_paid = db.Column(db.Numeric)
    # Has this payout information been paid? Used to determine how many share
    # slices to keep
    paid = db.Column(db.Boolean, default=False)


class Block(base):
    """ This class stores metadata on all blocks found by the pool """
    # the hash of the block for orphan checking
    hash = db.Column(db.String(64), primary_key=True)
    height = db.Column(db.Integer, nullable=False)
    # User who discovered block
    user = db.Column(db.String)
    worker = db.Column(db.String)
    # When block was found
    found_at = db.Column(db.DateTime, nullable=False)
    # # Time started on block
    time_started = db.Column(db.DateTime, nullable=False)
    # Is block now orphaned?
    orphan = db.Column(db.Boolean, default=False)
    # Is the block matured?
    mature = db.Column(db.Boolean, default=False)
    # Block total value (includes transaction fees)
    total_value = db.Column(db.Numeric)
    # Associated transaction fees
    transaction_fees = db.Column(db.Numeric)
    # Difficulty of block when solved
    difficulty = db.Column(db.Float, nullable=False)
    # 3-8 letter code for the currency that was mined
    currency = db.Column(db.String, nullable=False)
    # Will be == currency if currency is was merge mined
    merged = db.Column(db.Boolean, nullable=False)
    # The hashing algorith mused to solve the block
    algo = db.Column(db.String, nullable=False)

    standard_join = ['status', 'merged', 'currency', 'worker', 'explorer_link',
                     'luck', 'total_value_float', 'difficulty', 'duration',
                     'found_at', 'time_started']

    def __str__(self):
        return "<{} h:{} hsh:{}>".format(self.currency, self.height, self.hash)

    @property
    def contributed(self):
        # Total fees + donations associated with this block
        return sum([bp.contributed for bp in self.block_payouts]) or 0

    @property
    def bonus_paid(self):
        # Total fees + donations associated with this block
        return sum([bp.bonus_paid for bp in self.block_payouts]) or 0

    @property
    def shares_to_solve(self):
        # Total shares that were required to solve the block
        return sum([bp.shares for bp in self.block_payouts])

    @property
    def hr_shares_to_solve(self):
        return int(self.shares_to_solve)

    @property
    def status(self):
        if self.mature:
            return "Mature"
        if self.orphan:
            return "Orphan"
        confirms = self.confirms_remaining
        if confirms is not None:
            return "{} Confirms Remaining".format(confirms)
        else:
            return "Pending confirmation"

    @property
    def explorer_link(self):
        # XXX: Currently not supported
        return False

    @property
    def luck(self):
        hps = current_app.algos[self.algo].hashes_per_share
        return ((self.difficulty * (2 ** 32)) / ((float(self.shares_to_solve) or 1) * hps)) * 100

    @property
    def total_value_float(self):
        return float(self.total_value)

    @property
    def timestamp(self):
        return calendar.timegm(self.block.found_at.utctimetuple())

    @property
    def duration(self):
        seconds = round((self.found_at - self.time_started).total_seconds())
        formatted_time = timedelta(seconds=seconds)
        return formatted_time

    @property
    def confirms_remaining(self):
        return None


class Transaction(base):
    txid = db.Column(db.String(64), primary_key=True)
    confirmed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    currency = db.Column(db.String)


class Payout(base):
    """ A payout for currency directly crediting a users balance. These
    have no intermediary exchanges. """
    id = db.Column(db.Integer, primary_key=True)
    blockhash = db.Column(db.String(64), db.ForeignKey('block.hash'))
    block = db.relationship('Block', foreign_keys=[blockhash], backref='payouts')
    user = db.Column(db.String)
    sharechain_id = db.Column(db.SmallInteger)
    payout_address = db.Column(db.String)
    payout_currency = db.Column(db.String)
    amount = db.Column(db.Numeric, CheckConstraint('amount > 0', 'min_payout_amount'))
    fee_perc = db.Column(db.Numeric)
    pd_perc = db.Column(db.Numeric)
    type = db.Column(db.SmallInteger)
    payable = db.Column(db.Boolean, default=False)

    aggregate = db.relationship('PayoutAggregate', backref='payouts')
    aggregate_id = db.Column(db.Integer, db.ForeignKey('payout_aggregate.id'))

    __table_args__ = (
        db.Index('payable_idx', 'payable'),
    )

    __mapper_args__ = {
        'polymorphic_identity': 0,
        'polymorphic_on': type
    }

    standard_join = ['status', 'created_at', 'explorer_link',
                     'text_perc_applied', 'mined', 'amount_float', 'height',
                     'transaction_id']

    @property
    def currency(self):
        return self.payout_currency

    @property
    def payout_currency_obj(self):
        return currencies[self.payout_currency]

    @property
    def amount_float(self):
        return float(self.amount)

    @property
    def sharechain_title(self):
        return current_app.powerpools[self.pp_sharechain_id].title

    @property
    def cut_perc(self):
        return self.pd_perc + self.fee_perc

    @property
    def hr_fee_perc(self):
        return round((float(self.fee_perc) * 100), 2)

    @property
    def hr_pd_perc(self):
        return round((float(self.pd_perc) * 100), 2)

    @property
    def perc_applied(self):
        return (self.cut_perc * self.mined).quantize(current_app.SATOSHI)

    @property
    def text_perc_applied(self):
        if self.cut_perc < 0:
            return "BONUS {}".format(sig_round(self.perc_applied))
        else:
            return "{}".format(sig_round(self.perc_applied * -1))

    @property
    def mined(self):
        return self.amount / (1 - self.cut_perc)

    @classmethod
    def create(cls, user, amount, block, fee_perc, pd_perc, sharechain_id,
               payout_currency, payout_address=None):
        payout = cls(user=user, amount=amount, block=block,
                     fee_perc=fee_perc, pd_perc=pd_perc,
                     sharechain_id=sharechain_id,
                     payout_address=payout_address or user,
                     payout_currency=payout_currency)
        db.session.add(payout)
        return payout

    @property
    def height(self):
        return self.block.height

    @property
    def status(self):

        if self.block.orphan:
            return "Block Orphaned"

        if not self.block.mature:
            return "Pending Block Confirmation"

        if self.payable:
            if self.aggregate:
                if self.aggregate.transaction:
                    if self.aggregate.transaction.confirmed is True:
                        return "Payout Transaction {} Confirmed".\
                            format(self.aggregate.transaction.txid)
                    else:
                        return "Payout Transaction {} Pending".\
                            format(self.aggregate.transaction.txid)
                return "Payout Pending"
            return "Pending batching for payout"

    @property
    def final_amount(self):
        return self.amount


class PayoutExchange(Payout):
    """ A payout that needs a sale and a buy to get to the correct currency
    """
    id = db.Column(db.Integer, db.ForeignKey('payout.id'), primary_key=True)
    sell_req_id = db.Column(db.Integer, db.ForeignKey('trade_request.id'))
    sell_req = db.relationship('TradeRequest', foreign_keys=[sell_req_id],
                               backref='sell_payouts')
    sell_amount = db.Column(db.Numeric)
    buy_req_id = db.Column(db.Integer, db.ForeignKey('trade_request.id'))
    buy_req = db.relationship('TradeRequest', foreign_keys=[buy_req_id],
                              backref='buy_payouts')
    buy_amount = db.Column(db.Numeric)

    @property
    def status(self):

        if self.block.orphan:
            return "Block Orphaned"

        if not self.block.mature:
            return "Pending Block Confirmation"

        if self.payable:
            if self.aggregate:
                if self.aggregate.transaction:
                    if self.aggregate.transaction.confirmed is True:
                        return "Payout Transaction {} Confirmed".\
                            format(self.aggregate.transaction.txid)
                    else:
                        return "Payout Transaction {} Pending".\
                            format(self.aggregate.transaction.txid)
                return "Payout Pending"
            return "Pending batching for payout"

        if self.aggregate:
            if self.aggregate.transaction:
                if self.aggregate.transaction.confirmed is True:
                    return "Payout Transaction {} Confirmed".\
                        format(self.aggregate.transaction.txid)
                else:
                    return "Payout Transaction {} Pending".\
                        format(self.aggregate.transaction.txid)
            else:
                return "Pending batching for payout"

        # Don't say we're purchasing if we'd be shown as purchasing BTC to
        # avoid confusion
        btc = currencies.lookup_address(self.payout_address).key == "BTC"
        if self.buy_req and not btc:
            return "Purchasing desired currency"

        if self.sell_req and self.sell_req._status == 6:
            if btc:
                return "Pending Payout"
            else:
                return "Sold on exchange, pending purchase"

        return "Pending sale on exchange"

    @property
    def est_value(self):
        for version, currency_obj in current_app.currencies.iteritems():
            if self.currency == currency_obj.key:
                currency_ver = version
        return current_app.currencies[currency_ver].est_value(self.payout_currency_obj, self.amount)

    @property
    def final_amount(self):
        return self.buy_amount

    __mapper_args__ = {
        'polymorphic_identity': 1
    }


class PayoutAggregate(base):
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.String(64), db.ForeignKey('transaction.txid'))
    transaction = db.relationship('Transaction', backref='aggregates')
    user = db.Column(db.String)
    payout_address = db.Column(db.String, nullable=False)
    currency = db.Column(db.String, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    amount = db.Column(db.Numeric, CheckConstraint('amount > 0',
                                                   'min_aggregate_amount'))
    count = db.Column(db.SmallInteger)

    @property
    def payout_currency(self):
        return currencies[self.currency]

    @property
    def status(self):
        if self.transaction_id:
            return "Complete"
        return "Payout pending"

    @property
    def timestamp(self):
        return calendar.timegm(self.found_at.utctimetuple())

    @classmethod
    def create(cls, txid, user, amount, payout_address, count=None):
        aggr = cls(user=user,
                   payout_address=payout_address,
                   amount=amount,
                   count=count)
        db.session.add(aggr)
        return aggr


@classmethod
def average_combine(cls, *lst):
    """ Takes an iterable and combines the values. Usually either returns
    an average or a sum. Can assume at least one item in list """
    return sum(lst) / len(lst)


@classmethod
def sum_combine(cls, *lst):
    """ Takes a query list and combines the values. Usually either returns
    an average or a sum. Can assume at least one item in ql """
    return sum(lst)


class TimeSlice(object):
    """ An time abstracted data sample that pertains to a single worker.
    Currently used to represent accepted and rejected shares. """

    @property
    def end_time(self):
        return self.time + timedelta(
            seconds=self.span_config[self.span]['slice'].total_seconds())

    @property
    def item_key(self):
        return self.key(**{k: getattr(self, k) for k in self.keys})

    @classmethod
    def create(cls, user, worker, algo, value, time):
        dt = cls.floor_time(time)
        slc = cls(user=user, value=value, time=dt, worker=worker)
        db.session.add(slc)
        return slc

    @classmethod
    def add_value(cls, user, value, time, worker):
        dt = cls.floor_time(time)
        slc = cls.query.with_lockmode('update').filter_by(
            user=user, time=dt, worker=worker).one()
        slc.value += value

    @classmethod
    def floor_time(cls, time, span, stamp=False):
        seconds = cls.span_config[span]['slice'].total_seconds()
        if isinstance(time, datetime):
            time = calendar.timegm(time.utctimetuple())
        time = (time // seconds) * seconds
        if stamp:
            return int(time)
        return datetime.utcfromtimestamp(time)

    @classmethod
    def compress(cls, span, delete=True):
        # If we're trying to compress the largest slice boundary
        if span == len(cls.span_config) - 1:
            raise Exception("Can't compress this!")
        upper_span = span + 1

        # get the minute shares that are old enough to be compressed and
        # deleted
        recent = cls.floor_time(datetime.utcnow(), span) - cls.span_config[span]['window'] + cls.span_config[span]['slice']
        # the timestamp of the slice currently being processed
        current_slice = None
        # dictionary of lists keyed by item_hash
        items = {}

        def create_upper():
            # add a time slice for each user in this pending period
            for key, slices in items.iteritems():
                # Allows us to use different combining methods. Ie averaging or
                # adding
                new_val = cls.combine(*[slc.value for slc in slices])

                # put it in the database
                upper = cls.query.filter_by(time=current_slice, span=upper_span, **key._asdict()).with_lockmode('update').first()
                # wasn't in the db? create it
                if not upper:
                    upper = cls(time=current_slice, value=new_val, span=upper_span, **key._asdict())
                    db.session.add(upper)
                else:
                    upper.value = cls.combine(upper.value, new_val)

                if delete:
                    for slc in slices:
                        db.session.delete(slc)

        # traverse minute shares that are old enough in time order
        for slc in cls.query.filter(cls.time < recent).filter_by(span=span).order_by(cls.time):
            slice_time = cls.floor_time(slc.time, upper_span)

            if current_slice is None:
                current_slice = slice_time

            # we've encountered the next time slice, so commit the pending one
            if slice_time != current_slice:
                logging.debug("Processing slice " + str(current_slice))
                create_upper()
                items.clear()
                current_slice = slice_time

            # add the one min shares for this user the list of pending shares
            # to be grouped together
            key = slc.item_key
            items.setdefault(key, [])
            items[key].append(slc)

        create_upper()

    @classmethod
    def get_span(cls, lower=None, upper=None, stamp=False, ret_query=False,
                 slice_size=None, **kwargs):
        """ A utility to grab a group of slices and automatically compress
        smaller slices into larger slices

        address, worker, and algo are just filters.
        They may be a single string or list of strings.

        upper and lower are datetimes.
        """
        query = db.session.query(cls)

        # Allow us to filter by any of the keys
        for key in cls.keys:
            if key in kwargs:
                vals = kwargs.pop(key)
                if vals:
                    query = query.filter(getattr(cls, key).in_(vals))

        if kwargs:
            raise ValueError("Extra unused parameters {}".format(kwargs))

        if lower:
            query = query.filter(cls.time >= lower)

        # Attempt automatic slice size detection... Doesn't work too well
        if slice_size is None:
            if lower:
                query = query.filter(cls.time >= lower)
                # Determine which slice size we will use
                time_in_past = datetime.utcnow() - lower
                slice_size = None
                for i, cfg in enumerate(cls.span_config):
                    if cfg['window'] > time_in_past:
                        slice_size = i
                        break
                    slice_size = i
            else:
                slice_size = len(cls.span_config) - 1

        if upper:
            query = query.filter(cls.time <= upper)

        if ret_query:
            return query

        buckets = {}
        for slc in query:
            time = cls.floor_time(slc.time, slice_size, stamp=stamp)
            key = slc.item_key
            buckets.setdefault(key, {'data': slc.item_key._asdict(), 'values': {}})
            buckets[key]['values'].setdefault(time, 0)
            buckets[key]['values'][time] = cls.combine(buckets[key]['values'][time], slc.value)

        return buckets.values()


class ShareSlice(TimeSlice, base):
    SHARE_TYPES = ["acc", "low", "dup", "stale"]

    time = db.Column(db.DateTime, primary_key=True)
    user = db.Column(db.String, primary_key=True)
    worker = db.Column(db.String, primary_key=True)
    algo = db.Column(db.String, primary_key=True)
    share_type = db.Column(db.Enum(*SHARE_TYPES, name="share_type"),
                           primary_key=True)

    span = db.Column(db.SmallInteger, nullable=False)
    value = db.Column(db.Float)

    combine = sum_combine
    keys = ['user', 'worker', 'algo', 'share_type']
    key = namedtuple('Key', keys)
    span_config = [dict(window=timedelta(hours=1), slice=timedelta(minutes=1)),
                   dict(window=timedelta(days=1), slice=timedelta(minutes=5)),
                   dict(window=timedelta(days=30), slice=timedelta(hours=1))]


class DeviceSlice(TimeSlice, base):
    """ An data sample that pertains to a single workers device.  Currently
    used to temperature and hashrate. """

    time = db.Column(db.DateTime, primary_key=True)
    user = db.Column(db.String, primary_key=True)
    worker = db.Column(db.String, primary_key=True)
    device = db.Column(db.SmallInteger, primary_key=True)
    stat_val = db.Column(db.SmallInteger, nullable=False, primary_key=True)

    from_db = {0: "hashrate", 1: "temperature"}
    to_db = {"hashrate": 0, "temperature": 1}

    def get_stat(self, stat):
        return self.from_db[stat]

    def set_stat(self, stat):
        self.stat_val = self.to_db[stat]
    stat = property(get_stat, set_stat)
    __table_args__ = (
        db.Index('time_idx', 'time'),
    )

    span = db.Column(db.SmallInteger, nullable=False)
    value = db.Column(db.Float)

    combine = average_combine
    keys = ['user', 'worker', 'device', 'stat_val']
    key = namedtuple('Key', keys)
    span_config = [dict(window=timedelta(hours=1), slice=timedelta(minutes=1)),
                   dict(window=timedelta(days=1), slice=timedelta(minutes=5)),
                   dict(window=timedelta(days=30), slice=timedelta(hours=1))]


################################################################################
# User account related objects
################################################################################


class UserSettings(base):
    user = db.Column(db.String, primary_key=True)
    pdonation_perc = db.Column(db.Numeric, default=Decimal('0'))
    spayout_perc = db.Column(db.Numeric)
    spayout_addr = db.Column(db.String)
    spayout_curr = db.Column(db.String)
    anon = db.Column(db.Boolean, default=False)
    addresses = db.relationship("PayoutAddress")

    @property
    def exchangeable_addresses(self):
        return {pa_obj.currency: pa_obj.address for pa_obj in self.addresses if pa_obj.exchangeable}

    @property
    def unexchangeable_addresses(self):
        return {pa_obj.currency: pa_obj.address for pa_obj in self.addresses if not pa_obj.exchangeable}

    @property
    def hr_perc(self):
        return self.hr_pdonation_perc

    @property
    def hr_pdonation_perc(self):
        if self.pdonation_perc:
            return (self.pdonation_perc * 100).quantize(Decimal('0.01'))

    @property
    def hr_spayout_perc(self):
        if self.spayout_perc:
            return (self.spayout_perc * 100).quantize(Decimal('0.01'))

    @classmethod
    def update(cls, address, set_addrs, del_addrs, pdonate_perc, spayout_perc,
               spayout_addr, spayout_curr, del_spayout_addr, anon):

        user = cls.query.filter_by(user=address).first()
        if not user:
            UserSettings.create(address, pdonate_perc, spayout_perc,
                                spayout_addr, spayout_curr, del_spayout_addr,
                                anon, set_addrs)
        else:
            user.pdonation_perc = pdonate_perc
            user.anon = anon
            if del_spayout_addr:
                user.spayout_perc = None
                user.spayout_addr = None
                user.spayout_curr = None
            else:
                user.spayout_perc = spayout_perc
                user.spayout_addr = spayout_addr
                user.spayout_curr = spayout_curr

            # Set addresses
            for address in user.addresses:
                # Update existing
                for currency, addr in set_addrs.items():
                    if address.currency == currency:
                        address.currency = currency
                        address.address = addr
                        # Pop the currencies we just updated
                        set_addrs.pop(currency)
            # Add new currencies
            for currency, addr in set_addrs.iteritems():
                user.addresses.append(PayoutAddress.create(addr, currency))
            # Delete addresses
            for address in user.addresses:
                for currency in del_addrs:
                    if address.currency == currency:
                        db.session.delete(address)

        db.session.commit()
        return user

    @classmethod
    def create(cls, user, pdonate_perc, spayout_perc,  spayout_addr,
               spayout_curr, del_spayout_addr, anon, set_addrs):

        user = cls(user=user,
                   pdonation_perc=pdonate_perc,
                   anon=anon)
        if not del_spayout_addr:
            user.spayout_perc = spayout_perc
            user.spayout_addr = spayout_addr
            user.spayout_curr = spayout_curr
        db.session.add(user)

        for currency, addr in set_addrs.iteritems():
            user.addresses.append(PayoutAddress.create(addr, currency))

        return user


class PayoutAddress(base):
    address = db.Column(db.String, primary_key=True)
    user = db.Column(db.String, db.ForeignKey('user_settings.user'))
    # Abbreviated currency name. EG 'LTC'
    currency = db.Column(db.String)

    @classmethod
    def create(cls, address, currency):
        pa = cls(currency=currency,
                 address=address)
        db.session.add(pa)
        return pa

    @property
    def exchangeable(self):
        return currencies[self.currency].exchangeable
