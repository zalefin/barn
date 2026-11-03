"""On-demand prices for `list`, from the AWS Price List Query API
(pricing:GetProducts) with the account's own profile, cached in the barn home
for a week. When the API cannot be reached or the caller lacks the
permission (silently: it is optional), the rough us-east-1 table below
stands in."""

import json
import threading
import time

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import compat

# Rough us-east-1 on-demand $/hour, used when the Price List API is unavailable.
HOURLY = {
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "t3.large": 0.0832,
    "t3.xlarge": 0.1664,
    "m7i.large": 0.1008,
    "m7i.xlarge": 0.2016,
    "m7i.2xlarge": 0.4032,
    "m7i.4xlarge": 0.8064,
}
GP3_MONTH_PER_GB = 0.08

CACHE_FILE = "prices.json"
TTL = 7 * 86400
# After a failed lookup, a profile is not asked again for an hour, so `list`
# neither waits on a call that will fail again nor warns on every run.
FAILED_TTL = 3600
# The Price List Query API has endpoints only in us-east-1, eu-central-1 and ap-south-1.
ENDPOINT_REGION = "us-east-1"
# The role lacks pricing:GetProducts (it is optional): rough prices, no warning.
DENIED = {"AccessDeniedException", "AccessDenied"}
CLIENT_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2})

# Accounts are priced in parallel threads that share the cache file.
_lock = threading.Lock()


class Prices:
    """Prices in one account's region, looked up with that account's profile.
    home None keeps the cache in memory only."""

    def __init__(self, home, aws, now=time.time):
        self.path = home / CACHE_FILE if home else None
        self.aws = aws
        self.region = aws.account.region
        self.now = now
        # Why a lookup failed in this run ("Code: message"), for one warning;
        # a missing permission is not an error, only rough prices.
        self.error = None
        self.denied = False
        self._memory = {}
        self._client = None

    def hourly(self, instance_type):
        """$/hour for a Linux on-demand instance of this type, or None if unknown."""
        return self._price(
            f"{self.region}/{instance_type}",
            HOURLY.get(instance_type),
            unit="Hrs",
            instanceType=instance_type,
            regionCode=self.region,
            operatingSystem="Linux",
            tenancy="Shared",
            preInstalledSw="NA",
            capacitystatus="Used",
            operation="RunInstances",
        )

    def gp3_month_per_gb(self):
        """$/GB-month of gp3 storage."""
        return self._price(
            f"{self.region}/gp3",
            GP3_MONTH_PER_GB,
            unit="GB-Mo",
            productFamily="Storage",
            volumeApiName="gp3",
            regionCode=self.region,
        )

    def _price(self, key, fallback, unit, **attributes):
        now = self.now()
        cache = self._load()
        entry = cache.get(key)
        if entry and now - entry["at"] < TTL:
            return entry["usd"] if entry["usd"] is not None else fallback
        failed = cache.get(self._error_key)
        if self.error or self.denied or failed and now - failed["at"] < FAILED_TTL:
            return fallback
        try:
            usd = self._lookup(unit, attributes)
        except (ClientError, BotoCoreError) as e:
            err = e.response.get("Error", {}) if isinstance(e, ClientError) else {}
            message = f"{err.get('Code')}: {err.get('Message', '')}" if err else str(e)
            if err.get("Code") in DENIED:
                self.denied = True
            else:
                self.error = message
            self._store(self._error_key, {"at": now, "error": message})
            return fallback
        # Unknown in this region is cached too, so it is not asked again for a week.
        self._store(key, {"at": now, "usd": usd})
        return usd if usd is not None else fallback

    @property
    def _error_key(self):
        return f"error/{self.aws.profile}"

    def _lookup(self, unit, attributes):
        """The one on-demand USD price per unit for the product the attributes
        name, or None when there is no such product or more than one price."""
        if not self._client:
            self._client = self.aws.client("pricing", region_name=ENDPOINT_REGION, config=CLIENT_CONFIG)
        filters = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in attributes.items()]
        resp = self._client.get_products(ServiceCode="AmazonEC2", Filters=filters, MaxResults=100)
        found = set()
        for item in resp.get("PriceList", []):
            product = json.loads(item) if isinstance(item, str) else item
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    usd = float(dim.get("pricePerUnit", {}).get("USD", 0))
                    if dim.get("unit") == unit and usd > 0:
                        found.add(usd)
        return found.pop() if len(found) == 1 else None

    def _load(self):
        if not self.path:
            return self._memory
        with _lock:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}

    def _store(self, key, value):
        if not self.path:
            self._memory[key] = value
            return
        with _lock:
            try:
                cache = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
            cache[key] = value
            tmp = self.path.with_name(f".{self.path.name}.{threading.get_ident()}.tmp")
            try:
                compat.write_text(tmp, json.dumps(cache, indent=1, sort_keys=True) + "\n")
                compat.replace(tmp, self.path)
            except OSError:
                pass  # a cache that cannot be written only costs a lookup next time
