#[tokio::main]
async fn main() -> anyhow::Result<()> {
    verdigris_apps::service::run().await
}
