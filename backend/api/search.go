package api

import (
	"beatbump-server/backend/_youtube"
	"beatbump-server/backend/_youtube/api"
	"encoding/json"
	"fmt"
	"github.com/labstack/echo/v4"
	"net/http"
	"net/url"
	"strings"
)

var searchFilters = map[string]string{
	"all":                 "",
	"songs":               "EgWKAQIIAWoQEAoQBBAJEAUQAxAVEBAQEQ%3D%3D",
	"videos":              "EgWKAQIQAWoQEAoQBBAJEAUQAxAVEBAQEQ%3D%3D",
	"albums":              "EgWKAQIYAWoQEAoQBBAJEAUQAxAVEBAQEQ%3D%3D",
	"artists":             "EgWKAQIgAWoQEAoQBBAJEAUQAxAVEBAQEQ%3D%3D",
	"community_playlists": "EgeKAQQoAEABahAQChAEEAkQBRADEBUQEBAR",
	"featured_playlists":  "EgeKAQQoADgBagwQDhAKEAMQBBAJEAU%3D",
	"all_playlists":       "EgWKAQIoAWoKEAMQBBAKEAUQCQ%3D%3D",
}

// YouTube Music's unfiltered search response is not stable across clients and can
// omit the TabbedSearchResultsRenderer that Beatbump historically expected. For
// filter=all we aggregate the stable typed searches instead. This costs several
// upstream requests, but makes the public all-search endpoint deterministic.
var allSearchFilters = []string{"songs", "artists", "albums", "all_playlists", "videos"}

func SearchEndpointHandler(c echo.Context) error {
	urlQuery := c.Request().URL.Query()
	query := urlQuery.Get("q")
	filter := urlQuery.Get("filter")
	ctoken := urlQuery.Get("ctoken")
	itct := urlQuery.Get("itct")

	if query == "" && itct == "" && ctoken == "" {
		return c.String(http.StatusInternalServerError, "Missing required params")
	}
	queryUnescape, err := url.QueryUnescape(query)
	if err != nil {
		return c.String(http.StatusBadRequest, fmt.Sprintf("Invalid search query: %s", err))
	}

	if (filter == "all" || filter == "") && itct == "" && ctoken == "" {
		return handleAllSearch(c, queryUnescape)
	}
	filterID, ok := searchFilters[filter]
	if !ok {
		return c.String(http.StatusBadRequest, fmt.Sprintf("Unknown search filter: %s", filter))
	}
	return handleFilteredSearch(c, queryUnescape, filter, filterID, itct, ctoken)
}

func handleAllSearch(c echo.Context, query string) error {
	results := make([]MusicShelf, 0, len(allSearchFilters))
	var firstErr error

	for _, filter := range allSearchFilters {
		responseBytes, err := api.Search(query, searchFilters[filter], nil, nil, api.WebMusic)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		var searchResponse _youtube.SearchResponse
		if err = json.Unmarshal(responseBytes, &searchResponse); err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		if len(searchResponse.Content.TabbedSearchResultsRenderer.Tabs) == 0 {
			continue
		}
		searchContent := searchResponse.Content.TabbedSearchResultsRenderer.Tabs[0].TabRenderer.Content.SectionListRenderer.SectionListRendererContents
		shelves, err := parseResponse(searchContent)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		for i := range shelves {
			for j := range shelves[i].Contents {
				shelves[i].Contents[j].Type = filter
			}
		}
		results = append(results, shelves...)
	}

	if len(results) == 0 {
		if firstErr != nil {
			return c.String(http.StatusInternalServerError, fmt.Sprintf("Error building API request: %s", firstErr))
		}
		return c.String(http.StatusInternalServerError, "Search response contained no supported result renderer")
	}

	return c.JSON(http.StatusOK, struct {
		Results []MusicShelf `json:"results"`
	}{Results: results})
}

func handleFilteredSearch(c echo.Context, query, filter, filterID, itct, ctoken string) error {
	var responseBytes []byte
	var err error
	if itct != "" && ctoken != "" {
		responseBytes, err = api.Search(query, filterID, &itct, &ctoken, api.WebMusic)
	} else {
		responseBytes, err = api.Search(query, filterID, nil, nil, api.WebMusic)
	}
	if err != nil {
		return c.String(http.StatusInternalServerError, fmt.Sprintf("Error building API request: %s", err))
	}
	var searchResponse _youtube.SearchResponse
	if err = json.Unmarshal(responseBytes, &searchResponse); err != nil {
		return c.String(http.StatusInternalServerError, fmt.Sprintf("Error building API request: %s", err))
	}

	var regularResponse []MusicShelf
	var continuationResponse []IListItemRenderer
	var continuation _youtube.NextContinuationData
	var responseType *string
	if len(searchResponse.ContinuationContents.MusicShelfContinuation.Continuations) != 0 {
		searchContinuationContent := searchResponse.ContinuationContents.MusicShelfContinuation
		continuationResponse, err = parseContinuationResponse(searchContinuationContent.Content, filter)
		if len(searchContinuationContent.Continuations) == 1 {
			continuation = searchContinuationContent.Continuations[0].NextContinuationData
		}
		responseType = stringPtr("next")
	} else if len(searchResponse.Content.TabbedSearchResultsRenderer.Tabs) != 0 {
		searchContent := searchResponse.Content.TabbedSearchResultsRenderer.Tabs[0].TabRenderer.Content.SectionListRenderer.SectionListRendererContents
		regularResponse, err = parseResponse(searchContent)
		if len(searchContent) == 1 && searchContent[0].MusicShelfRenderer != nil && len(searchContent[0].MusicShelfRenderer.Continuations) != 0 {
			continuation = searchContent[0].MusicShelfRenderer.Continuations[0].NextContinuationData
		}
	} else {
		return c.String(http.StatusInternalServerError, "Search response contained no supported result renderer")
	}
	if err != nil {
		return c.String(http.StatusInternalServerError, fmt.Sprintf("Error parsing API response: %s", err))
	}

	if continuationResponse != nil {
		r := struct {
			ContinuationResults []IListItemRenderer            `json:"results"`
			Response            _youtube.SearchResponse        `json:"response"`
			Continuation        *_youtube.NextContinuationData `json:"continuation,omitempty"`
			Type                *string                        `json:"type,omitempty"`
		}{continuationResponse, searchResponse, &continuation, responseType}
		return c.JSON(http.StatusOK, r)
	}
	r := struct {
		Results      []MusicShelf                   `json:"results"`
		Response     _youtube.SearchResponse        `json:"response"`
		Continuation *_youtube.NextContinuationData `json:"continuation,omitempty"`
		Type         *string                        `json:"type,omitempty"`
	}{regularResponse, searchResponse, &continuation, responseType}
	return c.JSON(http.StatusOK, r)
}

func parseContinuationResponse(content []_youtube.MusicShelfContinuationContent, filter string) ([]IListItemRenderer, error) {
	response := make([]IListItemRenderer, 0, len(content))
	for _, entry := range content {
		item := parseMusicResponsiveListItemRenderer(entry.MusicResponsiveListItemRenderer)
		item.Type = filter
		response = append(response, item)
	}
	return response, nil
}

func parseResponse(content []_youtube.SectionListRendererContents) ([]MusicShelf, error) {
	response := make([]MusicShelf, 0, len(content))
	for _, shelf := range content {
		currShelf := MusicShelf{}
		if shelf.MusicShelfRenderer == nil {
			continue
		}
		title := ""
		if len(shelf.MusicShelfRenderer.Title.Runs) != 0 {
			title = shelf.MusicShelfRenderer.Title.Runs[0].Text
		}
		currShelf.Header.Title = title
		currShelf.Contents = make([]IListItemRenderer, 0, len(shelf.MusicShelfRenderer.Contents))
		for _, entry := range shelf.MusicShelfRenderer.Contents {
			item := parseMusicResponsiveListItemRenderer(entry.MusicResponsiveListItemRenderer)
			entryTitle := strings.ToLower(strings.ReplaceAll(title, " ", "_"))
			item.Type = entryTitle
			if entryTitle == "top_result" && item.Endpoint != nil {
				if strings.Contains(item.Endpoint.PageType, "SINGLE") || strings.Contains(item.Endpoint.PageType, "ALBUM") {
					item.Type = "albums"
				}
			}
			currShelf.Contents = append(currShelf.Contents, item)
		}
		response = append(response, currShelf)
	}
	return response, nil
}
