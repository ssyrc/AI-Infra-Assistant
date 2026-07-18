\c platform_config

-- dev 환경: vLLM 주소를 mock 서버로 덮어쓴다 (실제 GPU 없이 전체 흐름 검증용).
UPDATE platform_settings SET value = 'http://mock-vllm:8000/v1' WHERE key = 'vllm_llm_base_url';
UPDATE platform_settings SET value = 'http://mock-vllm:8000/v1' WHERE key = 'vllm_embed_base_url';
UPDATE platform_settings SET value = 'mock-llm'   WHERE key = 'vllm_llm_model';
UPDATE platform_settings SET value = 'mock-embed' WHERE key = 'vllm_embed_model';

-- dev에는 redis/리랭커가 없으므로 비활성화 (임베딩 캐시·리랭킹 생략).
UPDATE platform_settings SET value = '' WHERE key = 'redis_url';
UPDATE platform_settings SET value = '' WHERE key = 'rerank_base_url';
