import logging

from podimo.utils import randomFlyerId, generateHeaders as gHdrs, async_wrap

GRAPHQL_URL = "https://podimo.com/graphql"


class PodimoClient:
    def __init__(self, username: str, password: str, region: str, locale: str):
        self.username = username
        self.password = password
        self.region = region
        self.locale = locale
        self.token = None
        self.scraper = None

    def generateHeaders(self, authorization):
        return gHdrs(authorization, self.locale)

    async def post(self, headers, query, variables):
        response = await async_wrap(self.scraper.post)(
            GRAPHQL_URL,
            headers=headers,
            json={"query": query, "variables": variables},
            timeout=(6.05, 30),
        )
        if response is None:
            raise RuntimeError(f"No response for query: {query.strip()[:40]}...")
        if response.status_code != 200:
            raise RuntimeError(f"Podimo returned {response.status_code} for query: {query.strip()[:40]}...")
        result = response.json().get("data")
        if result is None:
            raise RuntimeError(f"Podimo returned no data for query: {query.strip()[:40]}")
        return result

    async def getPreregisterToken(self):
        headers = self.generateHeaders(None)
        query = """
            query AuthorizationPreregisterUser($locale: String!, $countryCode: String, $appsFlyerId: String) {
                tokenWithPreregisterUser(
                    locale: $locale
                    countryCode: $countryCode
                    source: MOBILE
                    appsFlyerId: $appsFlyerId
                    currentCountry: $countryCode
                ) {
                    token
                }
            }
        """
        variables = {"locale": self.locale, "countryCode": self.region, "appsFlyerId": randomFlyerId()}
        result = await self.post(headers, query, variables)
        self.preauth_token = result["tokenWithPreregisterUser"]["token"]
        if not self.preauth_token:
            raise RuntimeError("Podimo did not provide a preauth token")
        return self.preauth_token

    async def getOnboardingId(self):
        headers = self.generateHeaders(self.preauth_token)
        query = """
            query OnboardingQuery {
                userOnboardingFlow {
                    id
                }
            }
        """
        variables = {"locale": self.locale, "countryCode": self.region}
        result = await self.post(headers, query, variables)
        self.prereg_id = result["userOnboardingFlow"]["id"]
        return self.prereg_id

    async def podimoLogin(self):
        await self.getPreregisterToken()
        await self.getOnboardingId()
        headers = self.generateHeaders(self.preauth_token)
        query = """
            query AuthorizationAuthorize($email: String!, $password: String!, $locale: String!, $preregisterId: String) {
                tokenWithCredentials(
                    email: $email
                    password: $password
                    locale: $locale
                    preregisterId: $preregisterId
                ) {
                    token
                }
            }
        """
        variables = {
            "email": self.username,
            "password": self.password,
            "locale": self.locale,
            "preregisterId": self.prereg_id,
        }
        result = await self.post(headers, query, variables)
        token_data = result.get("tokenWithCredentials")
        if not token_data:
            raise ValueError("Invalid Podimo credentials — did not receive tokenWithCredentials")
        self.token = token_data["token"]
        if not self.token:
            raise ValueError("Invalid Podimo credentials — did not receive token")
        return self.token

    async def getPodcasts(self, podcast_id: str, limit: int = None):
        headers = self.generateHeaders(self.token)
        query = """
            query ChannelEpisodesQuery($podcastId: String!, $limit: Int!, $offset: Int!, $sorting: PodcastEpisodeSorting) {
                episodes: podcastEpisodes(
                    podcastId: $podcastId
                    converted: true
                    published: true
                    limit: $limit
                    offset: $offset
                    sorting: $sorting
                ) {
                    ...EpisodeBase
                }
                podcast: podcastById(podcastId: $podcastId) {
                    title
                    description
                    authorName
                    language
                    images {
                        coverImageUrl
                    }
                }
            }

            fragment EpisodeBase on PodcastEpisode {
                id
                artist
                podcastName
                imageUrl
                description
                datetime
                publishDatetime
                title
                audio {
                    url
                    duration
                }
                streamMedia {
                    duration
                    url
                }
            }
        """
        page_size = 100
        offset = 0
        full_result = None

        while True:
            variables = {
                "podcastId": podcast_id,
                "limit": page_size,
                "offset": offset,
                "sorting": "PUBLISHED_DESCENDING",
            }
            result = await self.post(headers, query, variables)
            if full_result is None:
                full_result = result
            else:
                full_result["episodes"] += result["episodes"]
            fetched = len(result["episodes"])
            logging.debug(f"Fetched {fetched} episodes at offset {offset}")
            if fetched < page_size:
                break
            offset += page_size

        if limit is not None:
            full_result["episodes"] = full_result["episodes"][:limit]

        return full_result
