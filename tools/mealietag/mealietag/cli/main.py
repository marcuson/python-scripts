import typer
import requests

cli = typer.Typer()

def get_headers(api_token: str):
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def get_or_create_tag(base_url: str, api_token: str, tag_name: str) -> dict:
    """Checks if a tag exists, otherwise creates it."""
    headers = get_headers(api_token)
    
    # Check existing tags
    response = requests.get(f"{base_url}/api/organizers/tags", headers=headers, params={"perPage": 500})
    response.raise_for_status()
    tags = response.json().get("items", [])
    
    for tag in tags:
        if tag["name"].lower() == tag_name.lower():
            return tag
            
    # Create tag if not found
    print(f"Tag '{tag_name}' not found. Creating it...")
    create_res = requests.post(
        f"{base_url}/api/organizers/tags", 
        headers=headers, 
        json={"name": tag_name}
    )
    create_res.raise_for_status()
    return create_res.json()

@cli.command()
def assign_tag(
    base_url: str = typer.Option(..., help="The base URL of your Mealie instance (e.g., http://192.168.1.10:9000)"),
    api_token: str = typer.Option(..., help="Your Mealie API Token"),
    tag_name: str = typer.Option("Untagged", help="The tag to assign to recipes without tags")
):
    """
    Finds all recipes in Mealie that have no tags and assigns a specific tag to them.
    """
    base_url = base_url.rstrip("/")
    headers = get_headers(api_token)

    try:
        # 1. Get or Create the target tag object
        target_tag = get_or_create_tag(base_url, api_token, tag_name)
        
        # 2. Fetch all recipes (handling pagination)
        print("Fetching recipes...")
        all_recipes = []
        page = 1
        while True:
            res = requests.get(
                f"{base_url}/api/recipes", 
                headers=headers, 
                params={"page": page, "perPage": 100}
            )
            res.raise_for_status()
            data = res.json()
            items = data.get("items", [])
            if not items:
                break
            all_recipes.extend(items)
            page += 1

        # 3. Identify recipes with no tags
        # Mealie 'tags' can be an empty list []
        to_tag_recipe_slugs = [r["slug"] for r in all_recipes if not r.get("tags")]

        if not to_tag_recipe_slugs:
            print("No recipes found to tag.")
            return

        print(f"Found {len(to_tag_recipe_slugs)} recipes to tag.")

        # 4. Use Bulk Action to assign the tag
        # The Bulk Tag endpoint expects a list of recipe IDs and the tag objects
        bulk_payload = {
            "recipes": to_tag_recipe_slugs,
            "tags": [target_tag]
        }
        
        bulk_res = requests.post(
            f"{base_url}/api/recipes/bulk-actions/tag",
            headers=headers,
            json=bulk_payload
        )
        bulk_res.raise_for_status()

        print(f"Successfully assigned tag '{tag_name}' to {len(to_tag_recipe_slugs)} recipes.")

    except requests.exceptions.HTTPError as e:
        typer.echo(f"Error: {e.response.text}", err=True)
        raise typer.Exit(code=1)
    except Exception as e:
        typer.echo(f"An error occurred: {e}", err=True)
        raise typer.Exit(code=1)

if __name__ == "__main__":
    cli()