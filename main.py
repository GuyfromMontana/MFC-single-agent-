curl -X PATCH "https://api.vapi.ai/assistant/9255ba03-4f63-4a98-b379-ebfff786fbb7" ^
  -H "Authorization: Bearer ca109987-f3ff-430d-8433-4fc74edc22eb" ^
  -H "Content-Type: application/json" ^
  -d "{\"serverUrl\": \"https://mfcagent-production.up.railway.app\", \"serverMessages\": [\"assistant-request\", \"end-of-call-report\"]}"