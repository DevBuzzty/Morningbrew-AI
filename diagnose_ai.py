
import os
import vertexai
from vertexai.generative_models import GenerativeModel

project_id = os.popen("gcloud config get-value project").read().strip()
regions = ["us-central1", "europe-west1", "europe-west3"]
models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]

print(f"--- DIAGNOSTICS FOR PROJECT: {project_id} ---")

for region in regions:
    print(f"\nTesting Region: {region}")
    try:
        vertexai.init(project=project_id, location=region)
        for model_name in models:
            try:
                model = GenerativeModel(model_name=model_name)
                # Just a tiny probe
                model.generate_content("Hi", generation_config={"max_output_tokens": 1})
                print(f"  [SUCCESS] {model_name} is available.")
            except Exception as e:
                print(f"  [FAILED ] {model_name}: {e}")
    except Exception as e:
        print(f"  [ERROR  ] Failed to init region {region}: {e}")

print("\n--- DIAGNOSTICS COMPLETE ---")
