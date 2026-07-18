-- MCP마다 완전히 분리된 DB. 같은 Postgres 인스턴스 안이지만 물리적으로 다른 DB이므로
-- 서로 다른 스키마 진화, 백업/권한 분리가 가능하다. 필요하면 각 DSN을
-- (관리자 콘솔 > 설정에서) 완전히 다른 Postgres 서버 주소로 바꿔도 코드 변경이 필요없다.
CREATE DATABASE platform_config;
CREATE DATABASE manual_db;
CREATE DATABASE voc_db;
CREATE DATABASE command_db;
CREATE DATABASE system_db;
CREATE DATABASE agent_sessions_db;
CREATE DATABASE langfuse;
