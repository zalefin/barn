"""Prices for `list` from the Price List API, cached, with the rough table as fallback."""

import json
from types import SimpleNamespace

from botocore.exceptions import ClientError, EndpointConnectionError
from test_boxes import FakePricing

from barn import pricing

DENIED = ClientError(
    {"Error": {"Code": "AccessDeniedException", "Message": "not authorized to perform: pricing:GetProducts"}},
    "GetProducts",
)


def make_aws(client, region="eu-west-1", profile="barn-zach-manager"):
    seen = []

    def make_client(name, **kw):
        seen.append((name, kw.get("region_name")))
        return client

    return SimpleNamespace(client=make_client, profile=profile, account=SimpleNamespace(region=region)), seen


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_prices_come_from_the_api_for_the_accounts_region(tmp_path):
    api = FakePricing({"m7i.xlarge": 0.2268, "gp3": 0.088})
    aws, seen = make_aws(api)
    prices = pricing.Prices(tmp_path, aws)
    assert prices.hourly("m7i.xlarge") == 0.2268
    assert prices.gp3_month_per_gb() == 0.088
    assert prices.error is None
    # The API's own endpoint region; the account's region is a filter.
    assert seen == [("pricing", "us-east-1")]
    assert api.calls[0] == {
        "instanceType": "m7i.xlarge",
        "regionCode": "eu-west-1",
        "operatingSystem": "Linux",
        "tenancy": "Shared",
        "preInstalledSw": "NA",
        "capacitystatus": "Used",
        "operation": "RunInstances",
    }
    assert api.calls[1] == {"productFamily": "Storage", "volumeApiName": "gp3", "regionCode": "eu-west-1"}


def test_prices_are_cached_for_a_week_across_runs(tmp_path):
    api = FakePricing({"m7i.xlarge": 0.2268})
    clock = Clock()
    aws, _ = make_aws(api)
    assert pricing.Prices(tmp_path, aws, clock).hourly("m7i.xlarge") == 0.2268
    cache = json.loads((tmp_path / pricing.CACHE_FILE).read_text())
    assert cache["eu-west-1/m7i.xlarge"]["usd"] == 0.2268

    api.prices["m7i.xlarge"] = 0.3
    clock.t += pricing.TTL - 1
    assert pricing.Prices(tmp_path, aws, clock).hourly("m7i.xlarge") == 0.2268
    assert len(api.calls) == 1
    clock.t += 2
    assert pricing.Prices(tmp_path, aws, clock).hourly("m7i.xlarge") == 0.3
    # Another region is another price.
    other, _ = make_aws(api, region="us-east-1")
    pricing.Prices(tmp_path, other, clock).hourly("m7i.xlarge")
    assert len(api.calls) == 3


def test_unknown_or_ambiguous_products_fall_back_to_the_table(tmp_path):
    api = FakePricing()
    aws, _ = make_aws(api)
    prices = pricing.Prices(tmp_path, aws)
    assert prices.hourly("m7i.large") == pricing.HOURLY["m7i.large"]
    assert prices.hourly("x9.huge") is None
    # Not found is cached too: asked once a week, not on every list.
    assert pricing.Prices(tmp_path, aws).hourly("x9.huge") is None
    assert len(api.calls) == 2

    dim = lambda usd: {"unit": "Hrs", "pricePerUnit": {"USD": usd}}
    two = {"terms": {"OnDemand": {"A": {"priceDimensions": {"1": dim("0.1"), "2": dim("0.2")}}}}}
    api.get_products = lambda **kw: {"PriceList": [json.dumps(two)]}
    assert pricing.Prices(None, aws).hourly("t3.small") == pricing.HOURLY["t3.small"]


def test_a_denied_lookup_falls_back_and_is_not_retried_for_an_hour(tmp_path):
    api = FakePricing({"t3.medium": 0.05}, error=DENIED)
    clock = Clock()
    aws, _ = make_aws(api)
    prices = pricing.Prices(tmp_path, aws, clock)
    assert prices.hourly("t3.medium") == pricing.HOURLY["t3.medium"]
    assert prices.gp3_month_per_gb() == pricing.GP3_MONTH_PER_GB
    assert prices.error.startswith("AccessDeniedException: not authorized")
    assert len(api.calls) == 1  # one failure stops the run's other lookups

    # The next run within the hour does not ask (and so does not warn again).
    later = pricing.Prices(tmp_path, aws, clock)
    assert later.hourly("t3.medium") == pricing.HOURLY["t3.medium"] and later.error is None
    assert len(api.calls) == 1
    # Another profile (another account) is asked.
    other, _ = make_aws(api, profile="barn-zach-work")
    pricing.Prices(tmp_path, other, clock).hourly("t3.medium")
    assert len(api.calls) == 2

    api.error = None
    clock.t += pricing.FAILED_TTL
    assert pricing.Prices(tmp_path, aws, clock).hourly("t3.medium") == 0.05


def test_network_errors_fall_back_too():
    api = FakePricing(error=EndpointConnectionError(endpoint_url="https://api.pricing.us-east-1.amazonaws.com"))
    aws, _ = make_aws(api)
    prices = pricing.Prices(None, aws)
    assert prices.hourly("t3.medium") == pricing.HOURLY["t3.medium"]
    assert "Could not connect" in prices.error


def test_an_unwritable_cache_only_costs_lookups(tmp_path):
    api = FakePricing({"t3.medium": 0.05})
    aws, _ = make_aws(api)
    (tmp_path / pricing.CACHE_FILE).mkdir()  # cannot be read or replaced
    assert pricing.Prices(tmp_path, aws).hourly("t3.medium") == 0.05
