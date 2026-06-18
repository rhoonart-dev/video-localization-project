"""pytest 경로 설정 — 프로젝트 루트를 import 경로에 추가."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
