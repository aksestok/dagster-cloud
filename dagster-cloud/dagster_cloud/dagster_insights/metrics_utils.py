import os
import tempfile
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union

import requests
from dagster import AssetExecutionContext, DagsterInstance, OpExecutionContext
from dagster._annotations import experimental
from dagster_cloud_cli.core.errors import raise_http_error
from dagster_cloud_cli.core.headers.auth import DagsterCloudInstanceScope
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport

from dagster_cloud.instance import DagsterCloudAgentInstance


@experimental
class DagsterMetric(NamedTuple):
    """Experimental: This class gives information about a Metric.

    Args:
        metric_name (str): name of the metric
        metric_value (float): value of the metric
    """

    metric_name: str
    metric_value: float


def query_graphql_from_instance(
    instance: DagsterInstance, query_text: str, variables=None
) -> Dict[str, Any]:
    headers = {}

    url, cloud_token = get_url_and_token_from_instance(instance)

    headers["Dagster-Cloud-API-Token"] = cloud_token

    transport = RequestsHTTPTransport(
        url=url,
        use_json=True,
        timeout=300,
        headers={"Dagster-Cloud-Api-Token": cloud_token},
    )
    client = Client(transport=transport, fetch_schema_from_transport=True)
    return client.execute(gql(query_text), variable_values=variables or dict())


def get_url_and_token_from_instance(instance: DagsterInstance) -> Tuple[str, str]:
    if not isinstance(instance, DagsterCloudAgentInstance):
        raise RuntimeError("This asset only functions in a running Dagster Cloud instance")

    return f"{instance.dagit_url}graphql", instance.dagster_cloud_agent_token


def get_post_request_params(
    instance: DagsterInstance,
) -> Tuple[requests.Session, str, Dict[str, str], int, Optional[Dict[str, str]]]:
    if not isinstance(instance, DagsterCloudAgentInstance):
        raise RuntimeError("This asset only functions in a running Dagster Cloud instance")

    return (
        instance.requests_managed_retries_session,
        instance.dagster_cloud_gen_insights_url_url,
        instance.dagster_cloud_api_headers(DagsterCloudInstanceScope.DEPLOYMENT),
        instance.dagster_cloud_api_timeout,
        instance.dagster_cloud_api_proxies,
    )


def upload_cost_information(
    context: Union[OpExecutionContext, AssetExecutionContext],
    metric_name: str,
    cost_information: List[Tuple[str, float, str]],
):
    import pyarrow as pa
    import pyarrow.parquet as pq

    with tempfile.TemporaryDirectory() as temp_dir:
        opaque_ids = pa.array([opaque_id for opaque_id, _, _ in cost_information])
        costs = pa.array([cost for _, cost, _ in cost_information])
        query_ids = pa.array([query_id for _, _, query_id in cost_information])
        metric_names = pa.array([metric_name for _, _, _ in cost_information])

        cost_pq_file = os.path.join(temp_dir, "cost.parquet")
        pq.write_table(
            pa.Table.from_arrays(
                [opaque_ids, costs, metric_names, query_ids],
                ["opaque_id", "cost", "metric_name", "query_id"],
            ),
            cost_pq_file,
        )

        instance = context.instance
        session, url, headers, timeout, proxies = get_post_request_params(instance)

        resp = session.post(url, headers=headers, timeout=timeout, proxies=proxies)
        raise_http_error(resp)
        resp_data = resp.json()

        assert "url" in resp_data and "fields" in resp_data, resp_data

        with open(cost_pq_file, "rb") as f:
            session.post(
                resp_data["url"],
                data=resp_data["fields"],
                files={"file": f},
            )


@experimental
def put_cost_information(
    context: Union[OpExecutionContext, AssetExecutionContext],
    metric_name: str,
    cost_information: List[Tuple[str, float, str]],
    start: float,
    end: float,
) -> None:
    try:
        upload_cost_information(context, metric_name, cost_information)
    except ImportError as e:
        raise Exception(
            "Dagster insights dependencies not installed. Install dagster-cloud[insights] to use this feature."
        ) from e
