# Copyright 2020 Cortex Labs, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import uvicorn
import yaml
import os
import json

from cortex.lib.type import (
    predictor_type_from_api_spec,
    PythonPredictorType,
    TensorFlowPredictorType,
    TensorFlowNeuronPredictorType,
    ONNXPredictorType,
)
from cortex.lib.model import (
    FileBasedModelsTreeUpdater,  # only when num workers > 1
    TFSModelLoader,
)
from cortex.lib.api import get_spec
from cortex.lib.checkers.pod import wait_neuron_rtd


def load_tensorflow_serving_models():
    # get TFS address-specific details
    model_dir = os.environ["CORTEX_MODEL_DIR"]
    tf_serving_host = os.getenv("CORTEX_TF_SERVING_HOST", "localhost")
    tf_base_serving_port = int(os.getenv("CORTEX_TF_BASE_SERVING_PORT", "9000"))

    # get models from environment variable
    models = os.environ["CORTEX_MODELS"].split(",")
    models = [model.strip() for model in models]

    from cortex.lib.server.tensorflow import TensorFlowServing

    # determine if multiple TF processes are required
    num_processes = 1
    has_multiple_tf_servers = os.getenv("CORTEX_MULTIPLE_TF_SERVERS")
    if has_multiple_tf_servers:
        num_processes = int(os.environ["CORTEX_PROCESSES_PER_REPLICA"])

    # initialize models for each TF process
    base_paths = [os.path.join(model_dir, name) for name in models]
    for w in range(int(num_processes)):
        tfs = TensorFlowServing(f"{tf_serving_host}:{tf_base_serving_port+w}")
        tfs.add_models_config(models, base_paths, replace_models=False)


def is_model_caching_enabled(api_spec: dir) -> bool:
    return (
        api_spec["predictor"]["models"]["cache_size"] is not None
        and api_spec["predictor"]["models"]["disk_cache_size"] is not None
    )


def main():
    with open("/src/cortex/serve/log_config.yaml", "r") as f:
        log_config = yaml.load(f, yaml.FullLoader)

    # wait until neuron-rtd sidecar is ready
    uses_inferentia = os.getenv("CORTEX_ACTIVE_NEURON")
    if uses_inferentia:
        wait_neuron_rtd()

    # strictly for Inferentia
    has_multiple_tf_servers = os.getenv("CORTEX_MULTIPLE_TF_SERVERS")
    if has_multiple_tf_servers:
        base_serving_port = int(os.environ["CORTEX_TF_BASE_SERVING_PORT"])
        num_processes = int(os.environ["CORTEX_PROCESSES_PER_REPLICA"])
        used_ports = {}
        for w in range(int(num_processes)):
            used_ports[str(base_serving_port + w)] = False
        with open("/run/used_ports.json", "w+") as f:
            json.dump(used_ports, f)

    # get API spec
    provider = os.environ["CORTEX_PROVIDER"]
    spec_path = os.environ["CORTEX_API_SPEC"]
    cache_dir = os.getenv("CORTEX_CACHE_DIR")  # when it's deployed locally
    bucket = os.getenv("CORTEX_BUCKET")  # when it's deployed to AWS
    region = os.getenv("AWS_REGION")  # when it's deployed to AWS
    _, api_spec = get_spec(provider, spec_path, cache_dir, bucket, region)

    predictor_type = predictor_type_from_api_spec(api_spec)
    multiple_processes = api_spec["predictor"]["processes_per_replica"] > 1
    caching_enabled = is_model_caching_enabled(api_spec)
    model_dir = os.environ["CORTEX_MODEL_DIR"]
    
    # create cron dirs if they don't exist
    if not caching_enabled:
        os.makedirs("/run/cron", exist_ok=True)
        os.makedirs("/tmp/cron", exist_ok=True)

    # start side-reloading when model caching not enabled > 1
    if not caching_enabled and predictor_type not in [
        TensorFlowPredictorType,
        TensorFlowNeuronPredictorType,
    ]:
        cron = FileBasedModelsTreeUpdater(
            interval=10,
            api_spec=api_spec,
            download_dir=model_dir,
        )
        cron.start()
    elif not caching_enabled and predictor_type == TensorFlowPredictorType:
        tf_serving_port = os.getenv("CORTEX_TF_BASE_SERVING_PORT", "9000")
        tf_serving_host = os.getenv("CORTEX_TF_SERVING_HOST", "localhost")
        cron = TFSModelLoader(
            interval=10,
            api_spec=api_spec,
            address=f"{tf_serving_host}:{tf_serving_port}",
            tfs_model_dir=model_dir,
            download_dir=model_dir,
        )
        cron.start()
    elif not caching_enabled and predictor_type == TensorFlowNeuronPredictorType:
        load_tensorflow_serving_models()
        cron = None

    # TODO if the cron is present, wait until it does its first pass

    # https://github.com/encode/uvicorn/blob/master/uvicorn/config.py
    uvicorn.run(
        "cortex.serve.wsgi:app",
        host="0.0.0.0",
        port=int(os.environ["CORTEX_SERVING_PORT"]),
        workers=int(os.environ["CORTEX_PROCESSES_PER_REPLICA"]),
        limit_concurrency=int(
            os.environ["CORTEX_MAX_PROCESS_CONCURRENCY"]
        ),  # this is a per process limit
        backlog=int(os.environ["CORTEX_SO_MAX_CONN"]),
        log_config=log_config,
        log_level="info",
    )


if __name__ == "__main__":
    main()
