import os
import logging
import uuid
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from middleware import AuthMiddleware

app = FastAPI()
app.add_middleware(AuthMiddleware)

# Kubernetes API Client setup
if "KUBERNETES_SERVICE_HOST" in os.environ:
    config.load_incluster_config()
else:
    config.load_kube_config()

k8s_api_client = client.ApiClient()

logging.basicConfig(level=logging.INFO)


# Request model for the POST endpoint
class ScrapeRequest(BaseModel):
    urls: list[str]  # List of URLs to scrape


@app.post("/api/ingest")
async def scrape_urls(request: ScrapeRequest):
    random_hash = uuid.uuid4().hex[:8]
    namespace = "default"
    configmap_name = f"scraper-configmap-{random_hash}"
    pvc_name = "shared-pvc"
    job_name = f"scraper-job-{random_hash}"

    
    # ConfigMap data: write URLs as a file-like structure
    configmap_data = {}
    filename = 'file'
    file_count = 0
    for url in request.urls:
        file_count += 1
        configmap_data[filename + str(file_count)] = url

    try:
        # Create ConfigMap
        core_v1 = client.CoreV1Api()
        configmap = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(name=configmap_name, namespace=namespace),
            data=configmap_data
        )
        try:
            core_v1.create_namespaced_config_map(namespace=namespace, body=configmap)
            logging.info(f"ConfigMap '{configmap_name}' created.")
        except ApiException as e:
            if e.status == 409:  # ConfigMap already exists
                logging.warning(f"ConfigMap '{configmap_name}' already exists, updating it.")
                core_v1.replace_namespaced_config_map(name=configmap_name, namespace=namespace, body=configmap)
            else:
                raise e

        # Define the Kubernetes Job
        create_sequential_job_with_configmap(
            job_name=job_name,
            namespace=namespace,
            pvc_name=pvc_name,
            configmap_name=configmap_name,
            scraper_image="scraper",
            vectorizer_image="vectorizer",
        )
        return {"message": f"Job '{job_name}' successfully created."}
    except ApiException as e:
        logging.error(f"Error interacting with Kubernetes: {e}")
        raise HTTPException(status_code=500, detail="Failed to create resources in Kubernetes")


def create_sequential_job_with_configmap(
    job_name: str,
    namespace: str,
    pvc_name: str,
    configmap_name: str,
    scraper_image: str,
    vectorizer_image: str,
):
    # core_v1 = client.CoreV1Api()
    batch_v1 = client.BatchV1Api()

    # Define the init container (scraper)
    init_container = client.V1Container(
        name="scraper",
        image=scraper_image,
        image_pull_policy="Never",
        volume_mounts=[
            client.V1VolumeMount(name="shared-volume", mount_path="/mnt/data"),
            client.V1VolumeMount(name="config-volume", mount_path="/mnt/config"),
        ],
    )

    # Define the main container (vectorizer)
    main_container = client.V1Container(
        name="vectorizer",
        image=vectorizer_image,
        image_pull_policy="Never",
        # command=[ "/bin/bash", "-c", "--" ],
        # args=["while true; do sleep 30; done;"],
        volume_mounts=[
            client.V1VolumeMount(name="shared-volume", mount_path="/mnt/data"),
        ],
    )

    # Define volumes
    shared_volume = client.V1Volume(
        name="shared-volume",
        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc_name),
    )
    config_volume = client.V1Volume(
        name="config-volume",
        config_map=client.V1ConfigMapVolumeSource(name=configmap_name),
    )

    # Pod template
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"job-name": job_name}),
        spec=client.V1PodSpec(
            restart_policy="Never",
            init_containers=[init_container],
            containers=[main_container],
            volumes=[shared_volume, config_volume],
        ),
    )

    # Job spec
    job_spec = client.V1JobSpec(template=template, backoff_limit=4)

    # Job definition
    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=job_name),
        spec=job_spec,
    )

    # Create the job
    try:
        batch_v1.create_namespaced_job(namespace=namespace, body=job)
        logging.info(f"Job '{job_name}' created successfully.")
    except ApiException as e:
        logging.error(f"Failed to create Job: {e}")
        raise
