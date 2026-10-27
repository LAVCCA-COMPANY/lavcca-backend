"""
aws/aws_handler.py - AWS 배포 핸들러
ECS Fargate (메인) + Lambda (경량 작업) + S3 (저장)
"""
import json
import os
import asyncio
import tempfile
from typing import Dict, Optional
from pathlib import Path
import structlog
import boto3
from botocore.exceptions import ClientError

from config import config
from pipeline import analyze_video_sync
from reports.report_generator import generate_html_report

logger = structlog.get_logger()


# ─────────────────────────────────────────────
# S3 핸들러
# ─────────────────────────────────────────────
class S3Handler:
    def __init__(self):
        self.s3 = boto3.client(
            "s3",
            region_name=config.aws.region,
            aws_access_key_id=config.api.aws_access_key_id,
            aws_secret_access_key=config.api.aws_secret_access_key,
        )
        self.bucket = config.aws.s3_bucket
        self.results_bucket = config.aws.s3_results_bucket

    def download_video(self, s3_key: str, local_path: str) -> str:
        """S3에서 영상 다운로드"""
        logger.info("s3_download_start", key=s3_key)
        self.s3.download_file(self.bucket, s3_key, local_path)
        size_mb = os.path.getsize(local_path) / (1024 * 1024)
        logger.info("s3_download_done", size_mb=f"{size_mb:.1f}MB")
        return local_path

    def upload_report(self, html_content: str, job_id: str) -> str:
        """HTML 리포트 S3 업로드"""
        key = f"reports/{job_id}/report.html"
        self.s3.put_object(
            Bucket=self.results_bucket,
            Key=key,
            Body=html_content.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )
        url = f"https://{self.results_bucket}.s3.{config.aws.region}.amazonaws.com/{key}"
        logger.info("report_uploaded", url=url)
        return url

    def upload_json_results(self, results: Dict, job_id: str) -> str:
        """JSON 결과 S3 업로드"""
        key = f"results/{job_id}/result.json"
        self.s3.put_object(
            Bucket=self.results_bucket,
            Key=key,
            Body=json.dumps(results, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return key

    def presigned_url(self, key: str, expires: int = 3600) -> str:
        """다운로드용 임시 URL"""
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.results_bucket, "Key": key},
            ExpiresIn=expires,
        )


# ─────────────────────────────────────────────
# ECS Task 트리거 (대용량 영상)
# ─────────────────────────────────────────────
class ECSHandler:
    def __init__(self):
        self.ecs = boto3.client("ecs", region_name=config.aws.region)

    def run_analysis_task(self, s3_key: str, job_id: str,
                          callback_url: Optional[str] = None) -> str:
        """
        ECS Fargate에서 분석 태스크 실행
        4vCPU / 8GB RAM 권장
        """
        env_vars = [
            {"name": "VIDEO_S3_KEY", "value": s3_key},
            {"name": "JOB_ID", "value": job_id},
        ]
        if callback_url:
            env_vars.append({"name": "CALLBACK_URL", "value": callback_url})

        response = self.ecs.run_task(
            cluster=config.aws.ecs_cluster,
            taskDefinition=config.aws.ecs_task_definition,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": os.environ.get("ECS_SUBNETS", "").split(","),
                    "securityGroups": os.environ.get("ECS_SECURITY_GROUPS", "").split(","),
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [{
                    "name": "copyright-detector",
                    "environment": env_vars,
                    "cpu": 4096,    # 4 vCPU
                    "memory": 8192, # 8 GB
                }]
            },
        )
        task_arn = response["tasks"][0]["taskArn"]
        logger.info("ecs_task_started", task_arn=task_arn, job_id=job_id)
        return task_arn


# ─────────────────────────────────────────────
# Lambda 핸들러 (경량 처리)
# ─────────────────────────────────────────────
def lambda_handler(event: Dict, context) -> Dict:
    """
    AWS Lambda 핸들러
    SQS → Lambda → ECS 패턴

    event 형식:
    {
        "s3_key": "videos/my_video.mp4",
        "job_id": "ABC123",
        "callback_url": "https://..."  # 선택적
    }
    """
    logger.info("lambda_handler_start", event=event)

    s3_key = event.get("s3_key")
    job_id = event.get("job_id")
    callback_url = event.get("callback_url")

    if not s3_key:
        return {"statusCode": 400, "body": "Missing s3_key"}

    s3 = S3Handler()

    # 임시 디렉토리에 영상 다운로드
    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, "video.mp4")
        s3.download_video(s3_key, local_path)

        # 분석 실행
        results = analyze_video_sync(local_path, job_id)

        # 결과 업로드
        html = generate_html_report(results)
        report_url = s3.upload_report(html, job_id)
        json_key = s3.upload_json_results(results, job_id)

        logger.info("lambda_complete",
                    job_id=job_id,
                    report_url=report_url,
                    findings=results["summary"]["total_issues_found"])

        # 콜백
        if callback_url:
            try:
                import requests
                requests.post(callback_url, json={
                    "job_id": job_id,
                    "status": "completed",
                    "report_url": report_url,
                    "summary": results["summary"],
                }, timeout=10)
            except Exception as e:
                logger.warning("callback_failed", error=str(e))

        return {
            "statusCode": 200,
            "body": json.dumps({
                "job_id": job_id,
                "report_url": report_url,
                "json_key": json_key,
                "summary": results["summary"],
            })
        }


# ─────────────────────────────────────────────
# SQS 메시지 처리 (배치)
# ─────────────────────────────────────────────
def sqs_handler(event: Dict, context) -> Dict:
    """SQS 트리거 Lambda 핸들러"""
    results = []
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            result = lambda_handler(body, context)
            results.append({"success": True, "result": result})
        except Exception as e:
            logger.error("sqs_record_failed", error=str(e))
            results.append({"success": False, "error": str(e)})
    return {"processed": len(results), "results": results}
