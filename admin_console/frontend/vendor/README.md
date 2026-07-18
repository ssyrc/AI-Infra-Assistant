# vendor/ - 폐쇄망 프론트엔드 의존성

인터넷이 되는 환경에서 아래 3개 파일을 받아 이 폴더에 그대로 넣고,
폐쇄망 빌드 서버로 옮기세요. index.html이 이 경로를 그대로 참조합니다.

```bash
curl -o react.production.min.js https://unpkg.com/react@18/umd/react.production.min.js
curl -o react-dom.production.min.js https://unpkg.com/react-dom@18/umd/react-dom.production.min.js
curl -o babel.min.js https://unpkg.com/@babel/standalone/babel.min.js
```

버전을 고정하고 싶으면 `react@18.3.1`처럼 태그를 지정하세요.
